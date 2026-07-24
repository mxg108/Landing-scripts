# ModelProviderDesign — abstracting the AI model behind the pipeline

**Status:** v1.1 — design only. **Scoring-scope update 2026-07-24:** §2's
"don't point Claude at scoring" is resolved by the two-stage split —
`TwoStageScoringDesign.md` makes the text leg of scoring the seam's
second consumer (Gemini annotates audio, any `TextModelProvider`
judges). The seam design below is unchanged and is that doc's P1.
**Date:** 2026-07-23 (v1), 2026-07-24 (v1.1)
**Prompted by:** "Our system abstracts Gemini config in the team JSON, but
how are we abstracting the AI model itself? I want to try Claude models
(not Fable — overkill) via API either for scoring itself or progression
services."

---

## 1. Where we actually stand

The honest answer: the **model name** is abstracted; the **provider** is
not.

| Layer | State today |
|---|---|
| Team JSON | `gemini.scoring_model`, `gemini.progression_model` + temperature/token knobs — swap `gemini-2.5-flash` for `gemini-2.5-pro` without touching code. |
| `audio_service.score_audio` | Hardcodes `google.genai.Client`, Gemini file-upload for audio, Gemini response parsing. |
| `progression_service` | Hardcodes `google.genai.Client` + `GEMINI_API_KEY`, Gemini generate_content, manual JSON-fence stripping. |
| `eval_store` | Stamps `ai_provider_primary = "gemini"` as a literal; `models_used` JSONB already carries `{provider, model}` per leg (§8.1) — the *schema* anticipated multi-provider, the writer doesn't. |

So we can change *which Gemini* per team, but trying Claude means editing
two service modules. That's exactly the state the RAG layer was in before
the `rag/` seam — and the fix is the same shape.

## 2. The constraint that decides "scoring vs progression"

**The Claude API has no audio input modality** (text, images, PDFs — no
audio files). Our scoring pipeline is audio-first by doctrine:

- Spanish calls: **audio is SOT** (owner rule 2026-07-19) — transcript-first
  scoring is explicitly forbidden wording;
- audio-dependent sections (tone, hold handling, pacing) are scored FROM
  the recording, with the transcript as a supplement.

A Claude scoring lane would therefore be transcript-only scoring — a
*doctrine change*, not a provider swap. Recommendation: **do not point
Claude at `score_audio`**. The natural first consumer is the
**progression / assessment service**: pure text in (serialized eval
history), JSON out, no audio anywhere — the same "first consumer" role
the scoring prompt played for Pulpo. The monthly one-pager
(`assessment_store`) is the second consumer, same shape.

(If LandGPT v2 ever splits scoring into an audio leg + text leg, the text
leg plugs into this same seam — `models_used.audio` / `.text` already
model that split.)

## 3. Design — `backend/services/llm/`, mirroring `rag/`

Same three-file pattern that made Pulpo swappable:

```
backend/services/llm/
  provider.py     # neutral contract — no vendor imports
  gemini.py       # the ONLY google-genai-shaped module (moves the
                  # client code out of progression_service)
  anthropic.py    # the ONLY anthropic-SDK-shaped module
  factory.py      # env/config-keyed singleton, mirrors get_rag_provider
```

### provider.py — the neutral contract

```python
@dataclass
class LlmResult:
    text: str                 # raw model text
    provider: str             # "gemini" | "anthropic" — for models_used
    model: str

class TextModelProvider(Protocol):
    async def generate(
        self, prompt: str, *,
        model: str,
        max_output_tokens: int,
        json_schema: dict | None = None,   # structured output when supported
    ) -> LlmResult: ...
```

Deliberately text-only (`generate(prompt) -> text`): that covers
progression + assessments today without inventing an audio interface
nobody can implement twice. Temperature stays OUT of the contract —
current Claude models reject sampling params; each provider module maps
its own knobs.

### anthropic.py — Claude specifics

- Official `anthropic` SDK (async client), `ANTHROPIC_API_KEY` env.
- **Model:** `claude-opus-4-8` as the quality default (the current Opus;
  Fable excluded per owner call — overkill/pricing). Cost lever if
  needed later: `claude-sonnet-5`. Exact IDs, no date suffixes.
- Adaptive thinking on (`thinking={"type": "adaptive"}`); **no
  temperature/top_p** (removed on Opus 4.8 — 400 if sent).
- Structured output via `output_config={"format": {"type": "json_schema",
  "schema": ...}}` — this **replaces** progression's markdown-fence
  stripping entirely on the Claude path (guaranteed-valid JSON), and is
  the main quality-of-life win of the trial.
- Long histories: fine — 1M context; keep `max_tokens` ≈ current
  `progression_max_output_tokens`.

### factory.py + config

- Team JSON grows a per-stage provider knob, defaulting to today's
  behavior:

  ```jsonc
  "models": {
    "progression": { "provider": "gemini", "model": "gemini-2.5-flash" }
    // "scoring" stays implicitly gemini/audio until a doctrine change
  }
  ```

  Back-compat: when `models` is absent, synthesize it from the existing
  `gemini.*` block so no team JSON has to change on day one.
- Env override for the trial, house-pattern-gated:
  `PROGRESSION_MODEL_PROVIDER=anthropic` flips it fleet-wide without a
  config edit (same off-by-default instinct as `PULPO_SOP_MODE`).

### Call-site changes

- `progression_service` / `assessment_store`: replace the inline
  `genai.Client` block with `provider = get_llm_provider(stage="progression",
  config=config)` + `provider.generate(...)`; stamp `result.provider` /
  `result.model` into any persisted assessment metadata.
- `eval_store`: `ai_provider_primary` and `ModelsUsed.text.provider`
  read from the scorecard's actual provider instead of the `"gemini"`
  literal (no behavior change until a non-Gemini stage exists).
- `audio_service`: untouched.

## 4. Rollout

1. **P1** — seam + `gemini.py` extraction (pure refactor; progression
   output byte-identical; pytest checkpoint).
2. **P2** — `anthropic.py` + live smoke script (one real progression
   assessment for one agent, printed side-by-side with Gemini's).
3. **P3** — trial: flip `PROGRESSION_MODEL_PROVIDER=anthropic` on
   Railway for a week; compare assessments qualitatively (they're
   advisory prose, not scores — no formula risk); pick per-team defaults.
4. **Not in scope:** Claude in the scoring path (audio doctrine, §2) and
   any change to formula/score computation (progression output never
   feeds scores).

## 5. Open questions for the owner

1. Progression first, or the EOM assessment one-pager first? (Same seam;
   one-pager is lower-frequency → cheaper trial, but progression gives
   faster feedback.)
2. Billing: new Anthropic org/key under Landing, or existing account?
   Railway needs `ANTHROPIC_API_KEY` either way.
3. Comfortable with Opus-tier pricing ($5/$25 per MTok) for the trial, or
   start on Sonnet 5 ($3/$15, intro $2/$10 through 2026-08-31)?
