# TwoStageScoringDesign — Gemini annotates, any LLM scores

**Status:** v1.2 — P1 shipped (PR #140, Claude leg live-verified incl.
structured output); P2 shipped (Stage-A annotator + `annotate_only`).
**Date:** 2026-07-24 (v1), 2026-07-25 (v1.1), 2026-07-26 (v1.2)

**v1.2 amendments:**
- `SCORING_PIPELINE` gains **`annotate_only`** between `single` and the
  `two_stage*` modes: Stage A runs on every scored call and the
  annotation persists, while the scoring prompt stays untouched —
  inspectable audio evidence accumulates before any judge change. The
  `two_stage*` values are accepted early but run as annotate_only (with
  a warning) until P3 lands — never silently nothing.
- Anthropic key resolution: `ANTHROPIC_API_KEY` →
  `ANTHROPIC_API_KEY_LANDING` → `ANTHROPIC_API_KEY_PERSONAL` (the
  owner's funded interim key), skipping placeholder-length values so
  the first REAL key wins without .env edits when engineering delivers.
- Annotator model knob for the §10.4 flash-vs-pro trial: env
  `ANNOTATOR_MODEL` (default `gemini-2.5-flash`);
  `scripts/annotate_smoke.py --model both` produces the side-by-side
  files for the Spanish-manager spot-check.
**Owner mandate:** "To incorporate Claude models (until Anthropic releases
a model that can natively listen to audio) we now have to split the
scoring work into the two-stage process we have outlined in this repo …
This is the moment to do it."
**Companions:** `ModelProviderDesign.md` (the `llm/` provider seam — Stage
2's substrate), `landing-ai/LandGPT.md` (the original cascade),
`database/SQLMigration.md` §8.2–8.3a (the schema that has been waiting
for this), `ScorecardActionsDesign.md` (parallel workstream — see §9).

---

## 1. What this is — and why it's not a workaround

Today `score_audio` is one Gemini call: audio + rubric + grounding + SOP
in, scorecard out. Gemini was chosen precisely because it natively
listens to audio (audio-is-SOT doctrine); no Claude model can, so Claude
cannot take that call over.

But the repo already outlined the answer — the **LandGPT cascade**
(LandGPT.md, Architecture): an audio-as-data interpreter produces an
**annotated transcript** that captures what plain text misses (tone,
emotion, interruptions, pacing, holds), and a text model scores against
the rubric FROM that artifact. The schema shipped ahead of the code:

| Already in place | Where |
|---|---|
| `qa.evaluations.annotated_transcript` JSONB (NULL today) | migration 006, SQLMigration §8.2 |
| `AnnotatedTranscript` / `TranscriptTurn` Pydantic models | `backend/models/formula.py` |
| `models_used` audio-leg + text-leg + `FallbackInfo` | §8.1, `ModelsUsed` |
| Per-section `evaluation_sections.ai_provider` | §8.3 |
| The stage name `gemini_annotate_v1` | LateStageDesign.md (reserved) |
| The provider seam for the text leg | ModelProviderDesign.md `llm/` |

This design fills those slots with **cloud providers today**: Gemini
becomes the Stage-A annotator (the ears), and Stage B is any
`TextModelProvider` (the judge) — Claude first. When the Local-AI
cutover arrives, Qwen2-Audio slots into Stage A and Gemma into Stage B
as *per-leg provider swaps*, not a pipeline rewrite. Two birds: the
Claude trial ships now, and the LandGPT migration risk collapses.

**The independent win:** the annotated transcript is inspectable. When a
manager disputes an audio-dependent score, we can show the artifact the
judge saw — LandGPT.md's defensibility claim ("the annotated transcript
is inspectable in a way that 'the model listened and decided' never
can be") starts being true months before LandGPT itself.

## 2. Architecture

```
                       ┌────────────── Stage A (audio leg) ──────────────┐
 audio bytes ──────────►  annotate_audio (audio_service, Gemini)         │
 dialpad transcript ───►  prompt: language rule + annotation schema      │
 moments (full set) ───►  output: AnnotatedTranscript JSON               │
                       │  (schema_version = "gemini_annotate_v1")        │
                       └───────────────┬─────────────────────────────────┘
                                       │ persisted → qa.evaluations.annotated_transcript
                                       ▼
                       ┌────────────── Stage B (text leg) ───────────────┐
 CC grounding block ───►  score_annotated (scoring via llm/ seam)        │
 SOP context block ────►  prompt: rubric + rendered annotated transcript │
 rubric prompt ────────►  output: Scorecard JSON (structured output)     │
                       └───────────────┬─────────────────────────────────┘
                                       ▼
                          ScorecardWithMeta → eval_store (unchanged shape)
```

- `score_call`'s **signature and return type do not change** — the split
  is internal. Rescore (ScorecardActionsDesign) re-runs whatever
  pipeline is active and REPLACEs `annotated_transcript` with the rest
  of the eval row.
- Grounding and SOP blocks move to Stage B only — the annotator doesn't
  score, so it doesn't need them. Stage A gets the transcript + full
  moments set (C0: filtering is a prompt decision) as *hints*, with the
  audio as SOT.
- `models_used` finally uses both legs:
  `audio = {provider: "gemini", model: <annotator>, version: "gemini_annotate_v1"}`,
  `text = {provider: "anthropic", model: <scorer>}`.
  `ai_provider_primary` = the text leg (the score author).

## 3. The annotated transcript — `gemini_annotate_v1`

Reuses §8.2's turn shape **verbatim** (schema_version exists exactly so
variants can extend it):

```jsonc
{
  "schema_version": "gemini_annotate_v1",
  "language_detected": "es",
  "turns": [
    { "speaker": "agent", "text": "...", "emotion": "neutral_friendly",
      "paraphrase_intent": "greeting + identity verification",
      "pace_marker": "normal", "interruption": false,
      "start_ms": 1200, "end_ms": 4800 }
  ],
  // NEW, additive (optional — Pydantic default []):
  "holds": [
    { "start_ms": 182000, "end_ms": 245000,
      "kind": "hold_music",            // hold_music | dead_air | mute_suspected
      "note": "agent announced hold before placing it" }
  ],
  "call_observations": [               // call-level, not per-turn
    "background noise on caller side throughout",
    "agent tone flattens after minute 12"
  ]
}
```

Model changes: `AnnotatedTranscript` gains `holds: list[HoldSegment] = []`
and `call_observations: list[str] = []` (additive; `extra="forbid"`
stays). §8.2's doc gets the same addendum.

### 3.1 Two kinds of hold truth — never conflate

| Source | Nature | May the prompt assert it as verified? |
|---|---|---|
| `holds[]` from Stage A | **Observational** — Gemini heard hold music / dead air | No — worded as observation ("audio suggests a hold of ~63s") |
| `command_center.hold_times` (webhook — owner's pending action) | **Verified system data** | Yes — rides the CC grounding block, gated by `has_hold_truth` exactly as today |

When the hold webhook lands, Stage B receives *both*: verified durations
in the grounding block and observational texture (was the hold
announced? dead air vs music?) in the annotation. The `has_hold_truth`
doctrine transfers unchanged — only webhook-observed calls get asserted
hold facts.

### 3.2 Spanish calls — how audio-is-SOT survives the split

Stage B never hears audio, so the SOT-carrier becomes the annotation.
The annotator prompt encodes the v2.1 language rule: for Spanish-audio
calls, Dialpad's transcript is unreliable and the annotator must
**re-transcribe from audio** (its `text` fields are its own hearing, not
a copy of Dialpad's), flagging divergences from the Dialpad transcript.
`language_detected` is stamped from audio, and Stage B's prompt states
that the annotated transcript IS the authoritative record of the call.
This is the same trust chain LandGPT commits to — worth validating in
the shadow week with a Spanish-speaking manager spot-check
(LandGPT.md's pilot gate, run early and cheap here).

## 4. Stage B — the judge, behind the `llm/` seam

Per ModelProviderDesign: `backend/services/llm/` with `provider.py` /
`gemini.py` / `anthropic.py` / `factory.py`. Stage B is its second
consumer (progression the first — same seam, no divergence).

- Prompt: rubric sections + scoring instructions (from TeamConfig, same
  builder pattern as today) + CC grounding block + SOP block + the
  **rendered** annotated transcript — a pure function turning the JSON
  into readable lines:
  `[03:02→04:05 HOLD (hold_music, observational) agent announced hold]`,
  `[00:01 agent | neutral_friendly, normal] "Buenos días..."`.
- Output: Scorecard JSON via structured output (Claude:
  `output_config.format` json_schema — kills fence-stripping; Gemini:
  `response_schema` equivalent) → same `Scorecard.model_validate`
  boundary as today.
- Default scorer: **`claude-sonnet-5`** (§10.2 — the starting tier; the
  newest Opus tier is reserved for progression analysis later; never
  Fable). Adaptive thinking on; no sampling params.
- Because Stage B is provider-agnostic, **Gemini can also be the judge**
  — useful for isolating "did the split change scores?" from "did
  Claude change scores?" (see §6 shadow design).

## 5. Failure semantics — single-stage is Plan B, not deleted

`score_audio` (today's single call) stays alive:

- Stage A failure (Gemini error, unparseable annotation) → log + fall
  back to single-stage `score_audio`; `models_used.fallback.reason =
  "annotate_failed"`; `annotated_transcript` NULL.
- Stage B failure (provider error, schema-invalid scorecard after
  retry) → same fallback, `reason = "text_scorer_failed"`.
- Fallback rows look exactly like today's rows — the pipeline never
  loses a scoring day to the new machinery (cc_context doctrine:
  enhancements are never scoring blockers).

§8.3a's per-section Plan B (confidence-based section rerouting) is
**out of scope for v1** — it's a LandGPT-era refinement; the schema
already supports it when wanted.

## 6. Config, gating, rollout

Team JSON grows (back-compat: absent → synthesized from `gemini.*`):

```jsonc
"models": {
  "scoring": {
    "pipeline": "single",                    // single | two_stage
    "annotator": { "provider": "gemini", "model": "gemini-2.5-flash" },
    "scorer":    { "provider": "anthropic", "model": "claude-sonnet-5" }
  },
  "progression": { "provider": "gemini", "model": "gemini-2.5-flash" }
}
```

Env override, house pattern (off by default):
`SCORING_PIPELINE = single | two_stage_shadow | two_stage`.

**Shadow mode** (the decision-making week): serve the single-stage
result exactly as today, AND run Stage A + Stage B on the same call;
persist the comparison into `dialpad_call_metadata.two_stage_shadow`
(per-section scores + deltas + scorer model) and the annotation into
`annotated_transcript` (real column — shadow annotations are themselves
the artifact we want to inspect). MS volume (~1 call/agent/day) makes
the doubled cost trivial for a week; rough per-call adder: annotation ≈
today's Gemini call, Claude Opus judge ≈ $0.15–0.30 (Sonnet ≈ 40% of
that).

Recommended sequence: shadow with **Gemini as judge first** (isolates
the split's effect on scores), then flip the judge to Claude (isolates
the model's effect). Two clean one-variable comparisons.

## 7. What changes where

| File | Change |
|---|---|
| `backend/services/llm/` (NEW) | seam per ModelProviderDesign — provider/gemini/anthropic/factory |
| `backend/services/audio_service.py` | + `annotate_audio()` (Stage A; own prompt, returns `AnnotatedTranscript`); `score_audio` untouched (Plan B) |
| `backend/services/scoring_service.py` | pipeline switch inside `score_call` (signature unchanged): single → today's path; two_stage → annotate → render → seam-score; shadow → both |
| `backend/models/formula.py` | `AnnotatedTranscript` + `holds`/`call_observations` (additive) |
| `backend/models/scorecard.py` | `ScorecardWithMeta` + `annotated_transcript`, + scorer provenance for `models_used` |
| `backend/services/eval_store.py` | persist `annotated_transcript`; `models_used` both legs; `ai_provider_primary` from text leg — **⚠ ScorecardActions territory, see §9** |
| Team JSONs + `team_config.py` | `models` block (defaulted; no team JSON edits required day one) |
| Tests | annotator-render pure-function tests; pipeline-switch tests with fake providers; fallback tests; grep-gate: nothing outside `llm/` imports `anthropic` |

Not touched: routes, frontend, GAS, formula/score computation, Sheets.

## 8. Build order (pytest checkpoint each)

- **P0** — owner answers (§10) + `ANTHROPIC_API_KEY` on Railway/local.
- **P1** — `llm/` seam + `gemini.py` extraction; progression flips onto
  the seam (pure refactor, byte-identical output). *ModelProviderDesign
  P1+P2 merged into this slice.*
- **P2** — Stage A: `annotate_audio` + schema extension + persistence +
  a lookup-triggered smoke on a handful of calls (incl. one Spanish,
  manager spot-check). **Valuable standalone** — inspectable audio
  evidence starts accumulating even before any judge change.
- **P3** — Stage B + `two_stage_shadow` (Gemini judge → Claude judge),
  comparison stamps, shadow-week report (PulpoConnection §6.1 pattern).
- **P4** — flip MS to `two_stage`; Sales stays `single` (July-rollout
  doctrine: Sales unchanged) until its own sign-off.
- **P5** — LandGPT convergence note in LandGPT.md: cutover = swap
  `annotator.provider` / `scorer.provider` per team.

## 9. Coordination — ScorecardActions session (parallel work)

An alternate session is building ScorecardActionsDesign S1–S6 (rescore =
REPLACE, override, delete) in `routes/scoring.py`, `eval_store.py`,
`scorecard.html`, migration 018. Overlap: `eval_store` row-dict and the
rescore path calling `score_call`.

Rules to keep the merge clean: this workstream **starts P1 (no
overlap) immediately; P2+ rebases onto the ScorecardActions branch once
it lands**; `score_call`'s signature stays frozen so rescore composes
with the pipeline switch for free; migration numbers — 018 is taken,
this design needs **no migration** (column 006, JSONB stamps only).

## 10. Open questions — RESOLVED (owner, 2026-07-25)

1. **Key/billing:** Landing has (or the owner can mint) an Anthropic API
   key; lands in `.env` + Railway ~Monday. Until then the anthropic
   provider raises a clean `LlmProviderError` and nothing selects it by
   default.
2. **Judge tier:** **`claude-sonnet-5` is the starting point.** The
   newest Opus tier (Opus 4.8 today; its successor when released) is
   reserved for **progression analysis** specifically, later. **Never
   Fable.**
3. **Shadow scope:** **all MS calls for a week** — volume makes it
   manageable and easy to extrapolate.
4. **Annotator model:** trial **both** `gemini-2.5-flash` AND
   `gemini-2.5-pro` — the P2 smoke runs the same calls through each and
   the Spanish spot-check compares annotations side by side.
5. **PII surface:** carried as **debt**, deliberately. The forcing
   function is the future **Agent view** — agents monitoring their own
   progress/performance — where raw transcriptions must NOT appear.
   That surface gets its own spec: **`AgentView.md`** (future branch +
   PR group); redaction/visibility rules for `annotated_transcript` are
   designed there, not here. Until then: rendered nowhere in the
   frontend, `KEY_ROLE_PRIVILEGED` gates raw reads (SQLMigration §8.4).
6. **Sequencing:** **go.** ScorecardActions doesn't touch model
   providers beyond what's already spec'd, so P1 starts immediately;
   the §9 rebase rule holds for P2+.
