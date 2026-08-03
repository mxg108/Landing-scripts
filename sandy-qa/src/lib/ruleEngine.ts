// Pure formula rule engine — port of backend/services/rule_engine.py.
// Table-driven scorer for the Ops-signed formula shape; one engine, both
// teams. NO DB, NO clock. Semantics preserved line-for-line:
//   - normalization: rating linear curve; binary Y/N map (per-section
//     binary_map overrides formula-level); NA per normalization.na
//     (full_credit → frac 1.0 | redistribute_per_rules → inactive, weight
//     must be moved off or UnhandledNaWeightError)
//   - evaluation_order tokens run exactly as listed; static misconfig
//     throws before any data is read
//   - when: equals compares String(answer); frac_lte/gte compare the
//     normalized PRE-redistribution frac; inactive section → numeric
//     conditions false, only equals:"NA" matches; signals default false
//   - score_override: final IS set_score; evaluation continues for trace;
//     later score_scale skipped
//   - answers strict: unknown/missing keys throw; NA only where allowed
// Final score clamped to formula.scale [min, max].

export type SectionAnswer = number | string;

const EPS = 1e-9;
const WEIGHTED_SUM = "weighted_sum";

const SCORE_TYPE_TRAITS: Record<string, [kind: string, naAllowed: boolean]> = {
  rating_1_5: ["rating", false],
  rating_1_5_na: ["rating", true],
  binary_yn: ["binary", false],
  binary_yn_na: ["binary", true],
  manual_yn: ["binary", true],
  auto_value: ["binary", false],
};

export class RuleEngineError extends Error {}

export interface ScoreResult {
  formula_id: string;
  rubric_version: string;
  final_score: number;
  raw_score: number;
  overridden: boolean;
  effective_weights: Record<string, number>;
  fracs: Record<string, number | null>;
  contributions: Record<string, number>;
  na_sections: string[];
  events: { rule_id: string; event: string; route: string | null }[];
  trace: { rule_id: string; rule_type: string; fired: boolean; note: string | null }[];
}

const PRE_SUM_ONLY = new Set(["weight_transfer", "weight_redistribution", "na_redistribution"]);
const POST_SUM_ONLY = new Set(["score_scale"]);

function checkStaticOrder(formula: any): void {
  const order: string[] = formula.evaluation_order;
  if (order.filter((t) => t === WEIGHTED_SUM).length !== 1)
    throw new RuleEngineError(
      `formula '${formula.formula_id}': evaluation_order must contain exactly one '${WEIGHTED_SUM}' token`
    );
  const sumAt = order.indexOf(WEIGHTED_SUM);
  const rulesById = new Map<string, any>(formula.rules.map((r: any) => [r.id, r]));
  order.forEach((token, pos) => {
    if (token === WEIGHTED_SUM) return;
    const rule = rulesById.get(token);
    if (!rule) throw new RuleEngineError(`evaluation_order token '${token}' has no rule`);
    if (PRE_SUM_ONLY.has(rule.type) && pos > sumAt)
      throw new RuleEngineError(
        `formula '${formula.formula_id}': ${rule.type} rule '${token}' after ${WEIGHTED_SUM}`
      );
    if (POST_SUM_ONLY.has(rule.type) && pos < sumAt)
      throw new RuleEngineError(
        `formula '${formula.formula_id}': ${rule.type} rule '${token}' before ${WEIGHTED_SUM}`
      );
  });
}

function ratingFrac(formula: any, section: any, answer: SectionAnswer): number {
  const curve = formula.normalization.rating_1_5;
  if (
    typeof answer !== "number" ||
    !Number.isInteger(answer) ||
    answer < curve.input_min ||
    answer > curve.input_max
  )
    throw new RuleEngineError(
      `section '${section.key}': expected int rating ${curve.input_min}-${curve.input_max}, got ${JSON.stringify(answer)}`
    );
  const [outLo, outHi] = curve.output;
  const span = curve.input_max - curve.input_min;
  return outLo + ((answer - curve.input_min) / span) * (outHi - outLo);
}

function binaryFrac(formula: any, section: any, answer: SectionAnswer): number {
  const map = section.binary_map ?? formula.normalization.binary_yn;
  if (answer === "Y") return map.Y;
  if (answer === "N") return map.N;
  throw new RuleEngineError(
    `section '${section.key}': expected 'Y'/'N', got ${JSON.stringify(answer)}`
  );
}

function normalizeAnswers(
  formula: any,
  answers: Record<string, SectionAnswer>
): [Record<string, number | null>, string[]] {
  const sectionKeys = new Set<string>(formula.sections.map((s: any) => s.key));
  const unknown = Object.keys(answers).filter((k) => !sectionKeys.has(k)).sort();
  if (unknown.length)
    throw new RuleEngineError(
      `formula '${formula.formula_id}': answers for unknown sections ${JSON.stringify(unknown)}`
    );
  const missing = [...sectionKeys].filter((k) => !(k in answers)).sort();
  if (missing.length)
    throw new RuleEngineError(
      `formula '${formula.formula_id}': missing answers for ${JSON.stringify(missing)}`
    );

  const fracs: Record<string, number | null> = {};
  const naSections: string[] = [];
  for (const section of formula.sections) {
    const answer = answers[section.key];
    const traits = SCORE_TYPE_TRAITS[section.score_type];
    if (!traits)
      throw new RuleEngineError(`section '${section.key}': unknown score_type '${section.score_type}'`);
    const [kind, naAllowed] = traits;
    if (answer === "NA") {
      if (!naAllowed)
        throw new RuleEngineError(
          `section '${section.key}': NA not allowed for score_type='${section.score_type}'`
        );
      naSections.push(section.key);
      fracs[section.key] = formula.normalization.na === "full_credit" ? 1.0 : null;
      continue;
    }
    fracs[section.key] =
      kind === "rating" ? ratingFrac(formula, section, answer) : binaryFrac(formula, section, answer);
  }
  return [fracs, naSections];
}

function whenMatches(
  when: any,
  answers: Record<string, SectionAnswer>,
  fracs: Record<string, number | null>,
  signals: Record<string, boolean>
): boolean {
  if (when.signal !== undefined) return signals[when.signal] ?? false;
  if (when.any !== undefined) return when.any.some((c: any) => whenMatches(c, answers, fracs, signals));
  if (when.all !== undefined) return when.all.every((c: any) => whenMatches(c, answers, fracs, signals));

  const hasCondition =
    when.equals !== undefined && when.equals !== null ||
    when.frac_lte !== undefined && when.frac_lte !== null ||
    when.frac_gte !== undefined && when.frac_gte !== null;
  if (!hasCondition)
    throw new RuleEngineError(`when-clause on section '${when.section}' has no condition`);
  if (!(when.section in fracs))
    throw new RuleEngineError(`when-clause references unknown section '${when.section}'`);
  if (when.equals !== undefined && when.equals !== null && String(answers[when.section]) !== when.equals)
    return false;
  const frac = fracs[when.section];
  if (when.frac_lte !== undefined && when.frac_lte !== null && (frac === null || frac > when.frac_lte))
    return false;
  if (when.frac_gte !== undefined && when.frac_gte !== null && (frac === null || frac < when.frac_gte))
    return false;
  return true;
}

function sectionsInWhen(when: any): Set<string> {
  if (when.section !== undefined) return new Set([when.section]);
  const clauses = when.any ?? when.all;
  if (clauses)
    return new Set(clauses.filter((c: any) => c.section !== undefined).map((c: any) => c.section));
  return new Set();
}

function redistributionTargets(
  selector: string | string[],
  exclude: Set<string>,
  weights: Record<string, number>,
  fracs: Record<string, number | null>,
  ruleId: string
): string[] {
  const pool =
    typeof selector === "string"
      ? Object.keys(weights).filter((k) => !exclude.has(k) && fracs[k] !== null && fracs[k] !== undefined)
      : selector.filter((k) => !exclude.has(k) && fracs[k] !== null && fracs[k] !== undefined);
  if (!pool.length)
    throw new RuleEngineError(`rule '${ruleId}': no active target sections to redistribute to`);
  return pool;
}

export function evaluateFormula(
  formula: any,
  answers: Record<string, SectionAnswer>,
  signals: Record<string, boolean> = {}
): ScoreResult {
  checkStaticOrder(formula);
  const [fracs, naSections] = normalizeAnswers(formula, answers);
  const weights: Record<string, number> = {};
  for (const s of formula.sections) weights[s.key] = s.weight;

  const rulesById = new Map<string, any>(formula.rules.map((r: any) => [r.id, r]));
  const trace: ScoreResult["trace"] = [];
  const events: ScoreResult["events"] = [];
  let overrideScore: number | null = null;
  let rawScore: number | null = null;
  let runningScore: number | null = null;

  for (const token of formula.evaluation_order) {
    if (token === WEIGHTED_SUM) {
      const stranded = Object.entries(fracs)
        .filter(([k, f]) => f === null && weights[k] > EPS)
        .map(([k]) => k);
      if (stranded.length)
        throw new RuleEngineError(
          `formula '${formula.formula_id}': NA sections still hold weight at weighted_sum time: ${JSON.stringify(stranded)}`
        );
      rawScore = Object.entries(fracs).reduce(
        (acc, [k, f]) => (f !== null ? acc + weights[k] * f : acc),
        0
      );
      runningScore = rawScore;
      continue;
    }

    const rule = rulesById.get(token)!;
    if (!rule.enabled) {
      trace.push({ rule_id: rule.id, rule_type: rule.type, fired: false, note: "disabled" });
      continue;
    }
    if (!whenMatches(rule.when, answers, fracs, signals)) {
      trace.push({ rule_id: rule.id, rule_type: rule.type, fired: false, note: null });
      continue;
    }

    let note: string | null = null;
    if (rule.type === "score_override") {
      overrideScore = rule.effect.set_score;
      note = `final score overridden to ${overrideScore}`;
    } else if (rule.type === "weight_transfer") {
      const effect = rule.effect;
      const source = effect.from;
      const moved =
        effect.amount === "all" ? weights[source] : weights[source] * (effect.fraction ?? 0);
      weights[source] -= moved;
      if (typeof effect.to === "string") {
        weights[effect.to] += moved;
        note = `moved ${moved} from '${source}' to '${effect.to}'`;
      } else {
        const shareSum = (Object.values(effect.to) as number[]).reduce((a, b) => a + b, 0);
        if (Math.abs(shareSum - 1.0) > 1e-6)
          throw new RuleEngineError(
            `rule '${rule.id}': transfer target shares sum to ${shareSum}, expected 1.0`
          );
        for (const [target, share] of Object.entries(effect.to))
          weights[target] += moved * (share as number);
        note = `moved ${moved} from '${source}' split`;
      }
    } else if (rule.type === "weight_redistribution") {
      const effect = rule.effect;
      const poolWeight = weights[effect.from];
      if (poolWeight <= EPS) {
        note = `no-op: '${effect.from}' holds no weight`;
      } else {
        const targets = redistributionTargets(effect.to, new Set([effect.from]), weights, fracs, rule.id);
        if (effect.method === "equal_additive") {
          const per = poolWeight / targets.length;
          for (const k of targets) weights[k] += per;
        } else {
          const base = targets.reduce((a, k) => a + weights[k], 0);
          if (base <= EPS)
            throw new RuleEngineError(`rule '${rule.id}': proportional redistribution but targets hold no weight`);
          const snapshot = { ...weights };
          for (const k of targets) weights[k] += poolWeight * snapshot[k] / base;
        }
        weights[effect.from] = 0.0;
        note = `spread ${poolWeight} from '${effect.from}'`;
      }
    } else if (rule.type === "na_redistribution") {
      const sources = [...sectionsInWhen(rule.when)]
        .sort()
        .filter((k) => (fracs[k] === null || fracs[k] === undefined) && (weights[k] ?? 0) > EPS);
      if (!sources.length) {
        note = "no-op: no NA sections with weight in when-clause";
      } else {
        const targets = redistributionTargets(rule.targets, new Set(sources), weights, fracs, rule.id);
        const base = targets.reduce((a, k) => a + weights[k], 0);
        if (base <= EPS)
          throw new RuleEngineError(`rule '${rule.id}': proportional redistribution but targets hold no weight`);
        const total = sources.reduce((a, k) => a + weights[k], 0);
        const snapshot = { ...weights };
        for (const k of targets) weights[k] += total * snapshot[k] / base;
        for (const k of sources) weights[k] = 0.0;
        note = `spread ${total} from NA sections`;
      }
    } else if (rule.type === "score_scale") {
      if (overrideScore !== null) {
        note = "skipped: score already overridden";
      } else {
        runningScore = (runningScore as number) * rule.effect.multiply;
        note = `score × ${rule.effect.multiply}`;
      }
    } else if (rule.type === "flag") {
      events.push({ rule_id: rule.id, event: rule.effect.emit_event, route: rule.effect.route ?? null });
      note = `emitted '${rule.effect.emit_event}'`;
    }
    trace.push({ rule_id: rule.id, rule_type: rule.type, fired: true, note });
  }

  if (rawScore === null || runningScore === null)
    throw new RuleEngineError("weighted_sum never ran");
  let final = overrideScore !== null ? overrideScore : runningScore;
  final = Math.min(Math.max(final, formula.scale.min), formula.scale.max);

  return {
    formula_id: formula.formula_id,
    rubric_version: formula.rubric_version,
    final_score: final,
    raw_score: rawScore,
    overridden: overrideScore !== null,
    effective_weights: weights,
    fracs,
    contributions: Object.fromEntries(
      Object.entries(fracs).map(([k, f]) => [k, f !== null ? weights[k] * f : 0.0])
    ),
    na_sections: naSections,
    events,
    trace,
  };
}

// overall_score NUMERIC(5,1) projection — Python quantizes HALF-UP on the
// decimal string (score_compute._quantize), NOT half-even.
export function quantizeScore(score: number): number {
  const s = Math.abs(score).toFixed(12);
  const dot = s.indexOf(".");
  const kept = s.slice(0, dot + 2);
  const rest = s.slice(dot + 2);
  let v = Math.round(parseFloat(kept) * 10);
  if (rest >= "5".padEnd(rest.length, "0")) v += 1;
  const out = v / 10;
  return score < 0 ? -out : out;
}
