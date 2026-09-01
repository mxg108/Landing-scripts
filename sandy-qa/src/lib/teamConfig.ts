// Team configuration from D1 — the worker-side equivalent of
// backend/config/team_config.py, sourced from the `teams` row (operational
// config, migration 010) and the team's CURRENT qa_rubric_versions row.

export interface SectionDef {
  id: string;
  history_id: string;
  name: string;
  section_number: number;
  score_type: string;
  score_range: [number, number] | null;
  audio_dependent: boolean;
  na_applicable: boolean;
  auto_value: string | null;
}

export interface StatsConfig {
  min_evals_for_outlier: number;
  outlier_z_threshold: number;
  ewma_span: number;
  ewma_min_evals: number;
  ewma_trend_delta: number;
  spc_sigma_multiplier: number;
}

export interface TeamConfig {
  team_id: string;
  company: string;
  provider: string; // 'dialpad' | 'retell' — call-provider seam (migration 0005)
  provider_config: any; // e.g. retell {agent_ids: [...]}; null for dialpad teams
  // Per-team doc-retrieval scope (migrations 0006+0013); null = unscoped.
  // {tags, match} allow-scopes a team to its doc families (sofia);
  // {exclude_tags} carves families out of the shared pool (MS/Sales
  // exclude system:sofia). Enforced on the tags each search hit carries
  // (sopRetrieval.applyTagScope). Provider-agnostic by design.
  retrieval_config: { tags?: string[]; match?: string; exclude_tags?: string[] } | null;
  // PromptConfig for the scoring prompt builders — raw rubric sections
  // (score_descriptions / na_applies_when / special_reasoning_instructions
  // included) + the rubric's scoring_prompt block.
  prompt_config: {
    company: string;
    scoring_prompt: any;
    sections: any[];
  };
  rubric_version: string;
  sections_by_number: SectionDef[];
  numeric_history_ids: string[];
  yn_history_ids: string[];
  section_labels: Record<string, string>;      // history_id -> name (numeric)
  yn_section_labels: Record<string, string>;   // history_id -> name (yn)
  history_id_to_section: Record<string, SectionDef>;
  stats: StatsConfig;
  excluded_test_agents: string[];
  archived_rubrics: any[]; // parsed rubric_json of EVERY rubric row (alias map)
}

// yn iff the score_type ends in "yn" (yn / manual_yn); numeric iff it carries
// a score_range (numeric / manual / manual_numeric). Mirrors team_config.py.
function isYn(s: any): boolean {
  return typeof s.score_type === "string" && s.score_type.endsWith("yn");
}
function isNumeric(s: any): boolean {
  return !isYn(s) && Array.isArray(s.score_range) && s.score_range.length === 2;
}

export async function loadTeamConfig(db: D1Database, teamId: string): Promise<TeamConfig> {
  const team = await db
    .prepare(
      "SELECT stats_config, excluded_test_agents, company, provider, provider_config, retrieval_config FROM teams WHERE id = ?"
    )
    .bind(teamId)
    .first<{
      stats_config: string | null;
      excluded_test_agents: string;
      company: string | null;
      provider: string | null;
      provider_config: string | null;
      retrieval_config: string | null;
    }>();
  if (!team) throw new Error(`unknown team ${teamId}`);

  // Alias-map iteration order = insertion order (id ASC) — matches the
  // Python oracle's unordered SELECT on a heap table, where setdefault makes
  // first-seen win. Current rubric = newest effective_from.
  const rubricRows = (
    await db
      .prepare(
        "SELECT rubric_json, effective_from FROM qa_rubric_versions WHERE team_id = ? ORDER BY id"
      )
      .bind(teamId)
      .all<{ rubric_json: string; effective_from: string }>()
  ).results;
  if (!rubricRows.length) throw new Error(`no rubric_versions row for ${teamId}`);
  const rubrics = rubricRows.map((r) => JSON.parse(r.rubric_json));
  const current =
    rubrics[
      rubricRows.reduce(
        (best, r, i) =>
          r.effective_from > rubricRows[best].effective_from ? i : best,
        0
      )
    ];

  const sections: SectionDef[] = (current.sections as any[])
    .map((s) => ({
      id: s.id,
      history_id: s.history_id ?? s.id,
      name: s.name,
      section_number: s.section_number,
      score_type: s.score_type,
      score_range: Array.isArray(s.score_range) ? (s.score_range as [number, number]) : null,
      audio_dependent: !!s.audio_dependent,
      na_applicable: !!s.na_applicable,
      auto_value: s.auto_value ?? null,
    }))
    .sort((a, b) => a.section_number - b.section_number);

  const numeric = sections.filter((s) => isNumeric(s));
  const yn = sections.filter((s) => isYn(s));

  const stats: StatsConfig = {
    min_evals_for_outlier: 5,
    outlier_z_threshold: 3.5,
    ewma_span: 5,
    ewma_min_evals: 3,
    ewma_trend_delta: 3.0,
    spc_sigma_multiplier: 2.0,
    ...(team.stats_config ? JSON.parse(team.stats_config) : {}),
  };

  const rawSections = [...(current.sections as any[])].sort(
    (a, b) => a.section_number - b.section_number
  );
  return {
    team_id: teamId,
    company: team.company ?? "Landing Living LLC",
    provider: team.provider ?? "dialpad",
    provider_config: team.provider_config ? JSON.parse(team.provider_config) : null,
    retrieval_config: team.retrieval_config ? JSON.parse(team.retrieval_config) : null,
    prompt_config: {
      company: team.company ?? "Landing Living LLC",
      scoring_prompt: current.scoring_prompt ?? {},
      sections: rawSections,
    },
    rubric_version: current.rubric_version,
    sections_by_number: sections,
    numeric_history_ids: numeric.map((s) => s.history_id),
    yn_history_ids: yn.map((s) => s.history_id),
    section_labels: Object.fromEntries(numeric.map((s) => [s.history_id, s.name])),
    yn_section_labels: Object.fromEntries(yn.map((s) => [s.history_id, s.name])),
    history_id_to_section: Object.fromEntries(sections.map((s) => [s.history_id, s])),
    stats,
    excluded_test_agents: JSON.parse(team.excluded_test_agents || "[]"),
    archived_rubrics: rubrics,
  };
}
