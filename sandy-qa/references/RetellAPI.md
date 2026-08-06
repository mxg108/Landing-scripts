# Retell AI — API reference + provider-seam design notes

*Researched 2026-08-06 from docs.retellai.com for the Sofia AI scoring slice
(Landing's in-testing Voice AI agent lives on Retell; we score its calls for
SOP adherence + human-likeness). Companion to
[[project_sofia_retell_provider]] (memory) and PortManifest.md.*

## 1. API contract (what we call)

Auth for everything: `Authorization: Bearer <RETELL_API_KEY>` (dashboard key).
Base: `https://api.retellai.com`.

### GET `/v2/get-call/{call_id}` — the workhorse

Returns the full call object (`V2PhoneCallResponse` | `V2WebCallResponse`).
Fields we care about:

| Field | Shape | Use in our pipeline |
|---|---|---|
| `call_id` | `call_…` string | primary ref (fits our TEXT call-id columns) |
| `agent_id` / `agent_name` / `agent_version` | string / string / int | which Sofia build took the call — version stamps matter while she's in testing |
| `call_status` | `registered\|not_connected\|ongoing\|ended\|error` | only score `ended` |
| `start_timestamp` / `end_timestamp` / `duration_ms` | epoch ms / epoch ms / int | `call_connected_at` / `call_ended_at` / `call_duration_ms` |
| `transcript` | plain string | reference-transcript block (audio stays SOT) |
| `transcript_object` | `[{role: agent\|user\|transfer_target, content, words: [{word, start, end}]}]` | word-level timestamps — richer than Dialpad's line times; feeds transcript_display + hold/silence hints |
| `recording_url` | S3 WAV URL | audio leg — Gemini accepts WAV. **Signed URLs expire in 24 h when `opt_in_signed_url`** → fetch fresh at trigger time, never persist |
| `recording_multi_channel_url` | S3 WAV (per-party channels) | prefer for the annotator if present — clean speaker separation |
| `call_analysis` | `{call_summary, in_voicemail, user_sentiment: Neg\|Pos\|Neutral\|Unknown, call_successful, custom_analysis_data}` | Retell's own post-call analysis = the CC-grounding analog (Sofia has no Dialpad dispositions) |
| `latency` | per-category `{p50,p90,p95,p99,max,min,num}`; categories `e2e,asr,llm,tts,knowledge_base,s2s` | **human-likeness gold**: objective response-gap stats to feed the annotator hints (a human doesn't answer in 80 ms or 4 s) |
| `disconnection_reason` | 40+ enum (`user_hangup`, `agent_hangup`, `voicemail_reached`, `inactivity`, `max_duration_reached`, `error_*`, …) | call-outcome context + auto-skip lists (voicemail etc.) |
| `from_number` / `to_number` / `direction` | phone-call variant | caller identity (`caller_phone`), inbound/outbound |
| `public_log_url` | string | the "open the call" link (our `dialpad_link` analog) — LLM req/resp + latency debug view |
| `metadata` / `retell_llm_dynamic_variables` / `collected_dynamic_variables` | objects | reservation/member context Sofia was given or collected — candidate grounding block |
| `disconnection_reason`, `transfer_destination` | | transfer handling (role `transfer_target` appears in transcript) |

HTTP: 200 ok, 401 bad key, 422 call not found.

### POST `/v3/list-calls` — discovery/backfill

v3 supersedes the deprecated v2/GET forms. Body:
`{ filter_criteria, sort_order: ascending|descending (by start_timestamp),
limit (≤1000, default 50), skip | pagination_key (mutually exclusive),
include_total }` → `{ items: [...], pagination_key, has_more, total? }`.

Filterable: `agent`(ids), `call_status`, `call_type`, `direction`,
`start_timestamp` ranges, `duration_ms`, `disconnection_reason`,
`user_sentiment`, `call_successful`, `in_voicemail`, `metadata`, more.

**⚠ v3 list items OMIT `transcript*` and `recording_url`** — always follow
with get-call per call. Pattern: hourly pull = list (`call_status: ended`,
`start_timestamp_gte` watermark) → get-call each → score.

## 2. Provider seam (how it lands in sandy-qa)

Today `src/routes/scoring.ts` hardcodes Dialpad (`getTranscript`,
`getCallDetails`, `DP` base). The seam:

```
src/lib/providers/
  types.ts     — CallProvider interface: fetchCall(ref) → NormalizedCall
  dialpad.ts   — extract of today's getTranscript/getCallDetails (no behavior change)
  retell.ts    — the new client (get-call v2 + list-calls v3)
```

`NormalizedCall` (the shape scoreTriggerInternal already consumes):
`{ call_id, transcript_text, transcript_display[], moments_display[],
caller_name, caller_phone, connected_at, ended_at, duration_ms,
entry_point_call_id?, review_link, audio: {url, format} | {provider:'dialpad'},
grounding: {…provider-specific block} }`

Provider selection by **team**: `sofia` team → retell, else dialpad
(teams config row carries `provider`; Sofia joins as team 3 — the greeting
page card already teases it).

Retell mapping decisions:
- **Audio leg**: app passes the fresh `recording_url` in the workflow
  payload; the workflow just downloads it (no Retell key needed
  workflow-side — unlike Dialpad). Prefer `recording_multi_channel_url`.
- **Grounding block**: `call_analysis` + `disconnection_reason` + latency
  summary replace the CC/disposition block; `metadata` +
  `collected_dynamic_variables` fold in when present.
- **Human-likeness hints**: pass `latency.e2e` percentiles + interruption/
  word-gap stats derived from `transcript_object` into the annotator
  prompt (the "Dialpad signal markers" slot generalizes to
  "platform signal markers").
- **SOP retrieval**: no dispositions → query Pulpo by
  `call_analysis.call_summary` + custom_analysis intent when the
  disposition key is absent (falls into the existing sub-τ conservative
  path when retrieval is weak).
- **Roster**: `sofia` team roster = one synthetic agent row per Retell
  `agent_id`+version? Start simple: single "Sofia" agent; stamp
  `agent_version` into `models_used`/metadata for per-build trend cuts.
  **Email dispatch: suppress for Sofia** (no inbox; scorecards live on
  dashboards; owner digest is a later choice).
- **Rubric**: new `sofia` rubric/formula versions (human-likeness +
  SOP sections) — separate design pass with the owner; the engine is
  config-driven so no code changes expected there.

## 3. Secrets

`RETELL_API_KEY` (15 chars — fits Sandy's 20-char cap): **app secret**
(Dashboard-set) — all Retell calls originate app-side; the workflow only
receives a pre-signed recording URL. Keys currently live in
qa-automation `.env` (Railway side, unused there for scoring).

## 4. Open questions for the slice

1. Sofia team id: `sofia` (teams table + KNOWN_TEAMS + greeting card flip).
2. Which Retell agent_ids are "Sofia prod-test" vs scratch agents —
   filter list-calls by agent ids (user provides).
3. Scoring cadence: manual console-first (paste call ids / a Retell
   lookup page variant), or auto-pull hourly? Start manual, add the
   hourly list-calls pull once rubric stabilizes.
4. `data_storage_setting` on Sofia's agent: if `everything_except_pii`,
   scrubbed fields replace raw ones — verify what her config stores.
