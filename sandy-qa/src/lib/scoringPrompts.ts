// Prompt builders — ports of backend/prompts/{annotator_prompt,
// qa_scoring_prompt, judge_prompt}.py. Text preserved byte-for-byte where
// Railway's prompts are byte-stable (rubric block, output schema, general
// rules) so two-stage and single-stage judges score against identical
// instructions on both stacks.
//
// Sections come from the CURRENT rubric_json (raw section objects — they
// carry score_descriptions / na_applies_when / special_reasoning_
// instructions that the SectionDef summary doesn't).

const SCHEMA_VERSION = "gemini_annotate_v1";

// Gemini response_schema for constrained decoding — hand-authored (the API's
// OpenAPI subset rejects additionalProperties).
export const ANNOTATOR_RESPONSE_SCHEMA = {
  type: "object",
  properties: {
    schema_version: { type: "string" },
    language_detected: { type: "string" },
    turns: {
      type: "array",
      items: {
        type: "object",
        properties: {
          speaker: { type: "string", enum: ["agent", "caller", "system", "other"] },
          text: { type: "string" },
          emotion: { type: "string" },
          paraphrase_intent: { type: "string" },
          pace_marker: { type: "string" },
          interruption: { type: "boolean" },
          start_ms: { type: "integer" },
          end_ms: { type: "integer" },
        },
        required: ["speaker", "text"],
      },
    },
    holds: {
      type: "array",
      items: {
        type: "object",
        properties: {
          start_ms: { type: "integer" },
          end_ms: { type: "integer" },
          kind: { type: "string", enum: ["hold_music", "dead_air", "mute_suspected"] },
          note: { type: "string" },
        },
        required: ["start_ms", "end_ms", "kind"],
      },
    },
    call_observations: { type: "array", items: { type: "string" } },
  },
  required: ["schema_version", "turns"],
};

export const ANNOTATOR_SYSTEM =
  "You are an audio-as-data interpreter for a QA pipeline at Landing, a " +
  "flexible-living company. You listen to member support / sales calls and " +
  "produce a structured ANNOTATED TRANSCRIPT. You never score, judge, or " +
  "coach — a separate evaluator does that using ONLY your annotation. " +
  "Anything you fail to capture from the audio is lost to the evaluator, " +
  "so be faithful and complete.";

const ANNOTATOR_INSTRUCTIONS = `=== YOUR TASK ===
Listen to the attached call audio and produce ONE JSON object with this
exact shape (schema_version "${SCHEMA_VERSION}"):

{
  "schema_version": "${SCHEMA_VERSION}",
  "language_detected": "<primary spoken language, ISO 639-1, e.g. "en"/"es">",
  "turns": [
    {
      "speaker": "agent" | "caller" | "system" | "other",
      "text": "<what was actually said — from the AUDIO>",
      "emotion": "<speaker's tone, e.g. neutral_friendly, frustrated, warm, flat, anxious>",
      "paraphrase_intent": "<one short phrase: what this turn is doing, e.g. 'greeting + identity verification'>",
      "pace_marker": "slow" | "normal" | "rushed",
      "interruption": <true when this turn cuts the other speaker off>,
      "start_ms": <int>, "end_ms": <int>
    }, ...
  ],
  "holds": [
    { "start_ms": <int>, "end_ms": <int>,
       "kind": "hold_music" | "dead_air" | "mute_suspected",
       "note": "<context, e.g. 'agent announced the hold before placing it'>" }
  ],
  "call_observations": [
    "<call-level observations a per-turn field can't carry: background noise,
     audio quality problems, overall tone arcs, long silences not worth a
     hold entry, notable divergences from the reference transcript>"
  ]
}

=== RULES ===
- THE AUDIO IS THE SOURCE OF TRUTH. The reference transcript below is a
  hint from an automatic transcriber; where it disagrees with what you
  hear, trust your ears and note material divergences in
  call_observations.
- If the call is NOT in English: the reference transcript is unreliable.
  Re-transcribe from the audio in the spoken language (do NOT translate
  turns to English), and be extra thorough with emotion/pace annotations.
- "turns.text" is verbatim speech. Do not summarize, censor, or clean it
  up beyond removing filler stutters.
- COLLAPSE filler and stutter runs: "uh uh uh...", "eh eh eh", repeated
  false starts — transcribe AT MOST one or two instances, never the full
  run. Prolonged stuttering or hesitation belongs in that turn's
  pace_marker or in call_observations, not spelled out token by token.
- "holds" are what you HEAR (music, dead air, suspected mute) — you are
  an observer, not a system of record. Include an entry for any gap over
  ~10 seconds. Do NOT record gaps shorter than 10 seconds — brief pauses
  are normal conversation, not holds.
- Timestamps are milliseconds from the start of the recording; best
  effort, never omitted.
- Mark "interruption": true only for genuine cut-offs, not backchannel
  ("mm-hm", "right").
- Output the JSON object ONLY — no markdown fences, no commentary.
`;

export function buildAnnotatorPrompt(
  transcriptText: string,
  momentsDisplay: any[] | null
): string {
  const parts = [ANNOTATOR_INSTRUCTIONS];
  if (momentsDisplay && momentsDisplay.length) {
    parts.push(
      "=== DIALPAD SIGNAL MARKERS (hints, machine-detected) ===\n" +
        JSON.stringify(momentsDisplay, null, 2)
    );
  }
  if (transcriptText.trim()) {
    parts.push(
      "=== REFERENCE TRANSCRIPT (hint — the audio overrules it) ===\n" +
        transcriptText.trim()
    );
  }
  return parts.join("\n\n");
}

// ── scoring / judge shared blocks ───────────────────────────────────────────

const LANDING_GENERAL_INSTRUCTIONS = `
=== GENERAL LANDING RULES ===
- Agent name in the greeting: an agent may introduce themselves with any
  name they choose (some teams have multiple people with the same first
  name, so reps may differentiate via a preferred or alternate name). Do
  NOT penalize the agent for the specific name used in the greeting. Do
  flag if the agent uses one name in the greeting and then switches to a
  different name later in the same call — consistency within the call is
  required. The agent's Dialpad-recorded name is internal and is NOT the
  ground truth for what name they must use with the lead/member.
- Source of truth: the audio is authoritative for scoring. If the call is
  detected as non-English, score from the audio content and treat the
  transcript as unreliable.`.replace(/\n$/, "");

const JUDGE_GENERAL_INSTRUCTIONS = `
=== GENERAL LANDING RULES ===
- Agent name in the greeting: an agent may introduce themselves with any
  name they choose (some teams have multiple people with the same first
  name, so reps may differentiate via a preferred or alternate name). Do
  NOT penalize the agent for the specific name used in the greeting. Do
  flag if the agent uses one name in the greeting and then switches to a
  different name later in the same call — consistency within the call is
  required. The agent's Dialpad-recorded name is internal and is NOT the
  ground truth for what name they must use with the lead/member.
- Source of truth: the ANNOTATED CALL RECORD is the authoritative
  account of the call. It was produced by an audio-native annotator
  that listened to the full recording — including for non-English
  calls, where the annotator re-transcribed from the audio. Score from
  the record; there is no separate transcript to consult.`.replace(/\n$/, "");

export interface PromptConfig {
  company: string;
  scoring_prompt: {
    system_prompt_template: string;
    confidence_levels_note: string;
    sop_sections: (number | string)[];
    long_call_focus_sections: string[];
  };
  sections: any[]; // raw rubric_json sections, sorted by section_number
}

function aiScoredSections(cfg: PromptConfig): any[] {
  return cfg.sections.filter(
    (s) => !["manual", "manual_yn"].includes(s.score_type) && !s.auto_value
  );
}

function renderTemplate(template: string, company: string): string {
  return template.replace(/\{company\}/g, company);
}

export function buildSystemPrompt(cfg: PromptConfig): string {
  return `${renderTemplate(cfg.scoring_prompt.system_prompt_template, cfg.company)}\n\n${LANDING_GENERAL_INSTRUCTIONS}`;
}

export function buildJudgeSystemPrompt(cfg: PromptConfig): string {
  return `${renderTemplate(cfg.scoring_prompt.system_prompt_template, cfg.company)}\n\n${JUDGE_GENERAL_INSTRUCTIONS}`;
}

export function buildScoringRubric(cfg: PromptConfig): string {
  const lines: string[] = ["\n=== SCORING RUBRIC ===\n"];
  for (const sec of cfg.sections) {
    if (sec.score_type === "manual" || sec.score_type === "manual_yn") {
      lines.push(
        `SECTION ${sec.section_number} — ${sec.name} (SKIP — always manual, never scored by AI)\n`
      );
      continue;
    }
    let header: string;
    if (sec.score_type === "yn") {
      header = `SECTION ${sec.section_number} — ${sec.name} (yn_value: "Y", "N", or "NA")`;
    } else {
      header = `SECTION ${sec.section_number} — ${sec.name} (score: ${sec.score_range[0]}–${sec.score_range[1]})`;
    }
    if (sec.audio_dependent) header += " [AUDIO-DEPENDENT]";
    lines.push(header);
    if (sec.rubric_question) lines.push(sec.rubric_question);
    if (sec.score_descriptions) {
      for (const [key, desc] of Object.entries(sec.score_descriptions)) {
        if (sec.score_type === "yn" && (key === "0" || key === "1")) {
          lines.push(`${key === "1" ? "Y" : "N"} (${key}): ${desc}`);
        } else {
          lines.push(`${key}: ${desc}`);
        }
      }
    }
    if (sec.na_applicable && (sec.score_type === "yn" || sec.score_type === "numeric")) {
      lines.push(
        sec.na_applies_when
          ? `NA policy: ${sec.na_applies_when}`
          : "Mark NA if not applicable (e.g. internal call or no sensitive info discussed)."
      );
    }
    lines.push("");
  }
  lines.push("=== CONFIDENCE LEVELS ===");
  lines.push(cfg.scoring_prompt.confidence_levels_note);
  lines.push("");
  const capped = aiScoredSections(cfg).filter((s) => s.confidence_cap);
  if (capped.length) {
    const numbers = capped.map((s) => String(s.section_number)).join(" and ");
    lines.push(
      `For Sections ${numbers}: cap confidence at "${capped[0].confidence_cap}" ` +
        "unless there is unambiguous audio evidence."
    );
  }
  return lines.join("\n");
}

export function buildOutputSchema(cfg: PromptConfig): string {
  const scored = aiScoredSections(cfg);
  const lines: string[] = [
    "\n=== REQUIRED OUTPUT FORMAT ===",
    "Return exactly this JSON structure and nothing else:\n",
    "{",
    '  "sections": [',
  ];
  scored.forEach((sec, i) => {
    const isLast = i === scored.length - 1;
    let scoreVal: string, ynField: string, scoreTypeStr: string;
    if (sec.score_type === "numeric") {
      const [lo, hi] = sec.score_range;
      scoreTypeStr = "numeric";
      if (sec.na_applicable) {
        scoreVal = `<${lo}-${hi} integer, or null if NA>`;
        ynField = '"<null or NA>"';
      } else {
        scoreVal = `<${lo}-${hi} integer>`;
        ynField = "null";
      }
    } else {
      scoreVal = "null";
      scoreTypeStr = "yn";
      ynField = sec.na_applicable ? '"<Y or N or NA>"' : '"<Y or N>"';
    }
    let reasoning = "<one or two sentences>";
    if (sec.special_reasoning_instructions)
      reasoning = `<one or two sentences, ${sec.special_reasoning_instructions}>`;
    lines.push("    {");
    lines.push(`      "id": "${sec.id}",`);
    lines.push(`      "name": "${sec.name}",`);
    lines.push(`      "score": ${scoreVal},`);
    lines.push(`      "score_type": "${scoreTypeStr}",`);
    lines.push(`      "yn_value": ${ynField},`);
    lines.push(`      "confidence": "<high or medium or low>",`);
    lines.push(`      "reasoning": "${reasoning}",`);
    lines.push(`      "audio_dependent": ${sec.audio_dependent ? "true" : "false"},`);
    lines.push(`      "flags": []`);
    lines.push(`    }${isLast ? "" : ","}`);
  });
  lines.push("  ],");
  lines.push('  "call_summary": "<a concise summary of the call, in one or two sentences>",');
  lines.push('  "key_strengths": "<2-3 specific strengths observed in this call, as a single string>",');
  lines.push('  "opportunities": "<2-3 specific coaching opportunities from this call, as a single string>"');
  lines.push("}");
  return lines.join("\n");
}

function sopSectionRefs(cfg: PromptConfig): string {
  const byId = new Map(cfg.sections.map((s) => [s.id, s]));
  const byNumber = new Map(cfg.sections.map((s) => [s.section_number, s]));
  const refs = cfg.scoring_prompt.sop_sections
    .map((sid) => (typeof sid === "number" ? byNumber.get(sid) : byId.get(sid)))
    .filter(Boolean)
    .map((s: any) => `Section ${s.section_number} (${s.name})`);
  if (!refs.length) return "the SOP-relevant sections";
  if (refs.length === 1) return refs[0];
  return refs.slice(0, -1).join(", ") + " and " + refs[refs.length - 1];
}

function sopMissingNote(cfg: PromptConfig): string {
  const refs = cfg.scoring_prompt.sop_sections.map(String).join(" and ");
  return (
    `\n[No SOP context loaded. Score Sections ${refs} ` +
    "conservatively and add 'sop_context_missing' to their flags.]\n"
  );
}

const ANNOTATION_CONTEXT_BLOCK = `
=== ANNOTATED CALL RECORD (authoritative) ===
You cannot hear the call. The record below is the authoritative account
of it, produced by an audio-native annotator. How to read it:
- Per-turn emotion / pace / [interrupts] tags are audio-derived
  observations — use them as your evidence for tone, empathy, and
  pacing judgments.
- HOLD lines are labeled "observational — not system-verified": the
  annotator HEARD them (music, dead air). Treat them as observations;
  only the CALL CONTEXT block above (when present) contains verified
  system data.
- CALL-LEVEL OBSERVATIONS carry context per-turn fields can't (audio
  quality, tone arcs, transcript divergences).
- An "ANNOTATION TRUNCATED" observation means the record is partial:
  score what the record shows, mark LOW confidence on any section the
  missing tail could change, and say so in that section's reasoning.

{{ANNOTATION_TEXT}}
`;

interface PromptExtras {
  sopTitle?: string;
  sopContent?: string;
  agentName?: string;
  extraNotes?: string;
  callContextText?: string;
  // One-sentence team framing so the judge knows WHICH line the call came
  // from (observed miss: an MS support-line call scored against a Sofia
  // SOP's locksmith prohibition). Interim until Pulpo per-team tag
  // scoping lands; rides both single-stage and judge prompts.
  teamContext?: string;
}

function sopBlock(cfg: PromptConfig, x: PromptExtras): string {
  if (x.sopContent) {
    return (
      `\n=== SOP CONTEXT (${x.sopTitle ?? ""}) ===\n` +
      `Use the following Standard Operating Procedure to evaluate ${sopSectionRefs(cfg)}.\n` +
      "Score these sections against this policy, not general knowledge.\n\n" +
      `${x.sopContent}\n`
    );
  }
  return sopMissingNote(cfg);
}

// Single-stage user prompt (audio attached) — build_prompt port.
export function buildScoringPrompt(
  cfg: PromptConfig,
  transcriptText: string,
  x: PromptExtras = {}
): string {
  const parts = [buildScoringRubric(cfg)];
  if (x.teamContext) parts.push(`\n${x.teamContext}`);
  parts.push(sopBlock(cfg, x));
  if (x.callContextText) parts.push(x.callContextText);
  if (transcriptText)
    parts.push(
      "\n=== DIALPAD TRANSCRIPT ===\n" +
        "Use this alongside the audio to improve accuracy. Speaker labels and content are from Dialpad.\n\n" +
        `${transcriptText}\n`
    );
  if (x.agentName) parts.push(`\nAgent name: ${x.agentName}`);
  if (x.extraNotes) parts.push(`Additional context: ${x.extraNotes}`);
  parts.push(buildOutputSchema(cfg));
  parts.push(
    "\n[Audio attached — score the call based on what you hear; the " +
      "transcript is secondary to the audio source. If the call is in " +
      "SPANISH, ignore the transcript and base your scores solely on the " +
      "audio. DO NOT mention a lack of SOP in your score reasoning if no " +
      "SOP context was provided.]"
  );
  return parts.join("\n");
}

// Judge user prompt TEMPLATE — the workflow substitutes {{ANNOTATION_TEXT}}
// with the rendered annotated record at judge time.
export function buildJudgePromptTemplate(cfg: PromptConfig, x: PromptExtras = {}): string {
  const parts = [buildScoringRubric(cfg)];
  if (x.teamContext) parts.push(`\n${x.teamContext}`);
  parts.push(sopBlock(cfg, x));
  if (x.callContextText) parts.push(x.callContextText);
  parts.push(ANNOTATION_CONTEXT_BLOCK);
  if (x.agentName) parts.push(`\nAgent name: ${x.agentName}`);
  if (x.extraNotes) parts.push(`Additional context: ${x.extraNotes}`);
  parts.push(buildOutputSchema(cfg));
  parts.push(
    "\n[No audio is attached. Score exclusively from the ANNOTATED " +
      "CALL RECORD and the context blocks above. DO NOT mention a lack " +
      "of SOP in your score reasoning if no SOP context was provided.]"
  );
  return parts.join("\n");
}

// Long-call note — scoring_service.py extra_notes logic.
export function buildLongCallNote(cfg: PromptConfig, durationMs: number): string {
  const FLAG_MS = 25 * 60 * 1000;
  if (!(durationMs > FLAG_MS)) return "";
  const byId = new Map(cfg.sections.map((s) => [s.id, s]));
  const focus = cfg.scoring_prompt.long_call_focus_sections
    .map((sid) => byId.get(sid))
    .filter(Boolean) as any[];
  if (focus.length) {
    const focusText = focus.map((s) => `${s.name} (Section ${s.section_number})`).join(", ");
    return (
      `NOTE: This call is over 25 minutes. Pay special attention to ${focusText}. ` +
      "Flag any unnecessary hold time, delays, or pacing issues, including timestamps. "
    );
  }
  return (
    "NOTE: This call is over 25 minutes. Pay special attention to " +
    "audio-dependent sections. Flag any unnecessary hold time, " +
    "delays, or pacing issues, including timestamps. "
  );
}
