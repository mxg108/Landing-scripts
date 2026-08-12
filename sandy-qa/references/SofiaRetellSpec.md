# Sofia AI on Sandy — Retell provider + third team (spec)

*Design doc for the Sofia slice, 2026-08-09. Companion to
[RetellAPI.md](RetellAPI.md) (API field map + seam sketch) and PortManifest.md.
Doc-first per working agreement; code follows sign-off on this spec.*

## 0. Goal

Add **Sofia AI** (Landing's voice agent, calls on **Retell.ai**) as the third
scored team (`sofia`) alongside `member_support` / `sales` — behind a
**call-provider seam** so no Dialpad assumption hardens further — and exploit
the Retell-only signals Dialpad never had: word-level timings, latency
percentiles, post-call analysis, multi-channel audio, per-build agent
versioning.

Scoring focus for Sofia (owner intent): **SOP adherence + human-likeness.**

## 1. What is Dialpad-shaped today (audit of the live code)

| Site | Today | Sofia problem |
|---|---|---|
| `src/routes/scoring.ts:31,49,98` | `getTranscript`/`getCallDetails` against `https://dialpad.com/api/v2` | no Retell path |
| `scoring.ts:170` | hard 503 unless `DIALPAD_API_KEY` set | wrong secret gate for a Retell team |
| `scoring.ts:224–240` | CC grounding (`fetchCallContext`) + SOP query keyed on **disposition** | Sofia has no CC rows / dispositions |
| `scoring.ts:244–256` | `teamLabel` ternary: `sales` else `Member Support` | Sofia would be told it's a human MS agent |
| `scoring.ts:296–316` | `persist.dialpad_*` fields, `dialpad_link` built as `dialpad.com/callhistory/...` (`:1313`) | need Retell equivalents |
| `workflows/qa-scoring-pipeline.js:199–217` | audio leg fetches Dialpad meta → `recording_details[0].url` + `apikey` param | Retell hands us a pre-signed WAV URL instead |
| `src/lib/emailDispatch.ts:25` | `teamId === "sales" ? sales : member_support` | ⚠ **BUG-IN-WAITING: `sofia` falls into the `member_support` branch and would POST scorecards to the MS GAS webapp.** Must become an explicit map before team 3 exists. |
| `src/routes/teamApi.ts:40` | `KNOWN_TEAMS = {member_support, sales}` | add `sofia` |
| `teamApi.ts:88` | greeting card "Sofia AI — Coming soon" | flip to Live |
| `src/lib/sopRetrieval.ts:166–169` | query = disposition, fallback = transcript head (600 chars) | Retell's `call_analysis.call_summary` is a far better query |
| D1 `teams` DDL (0001) | no provider notion | migration 0005 |

Everything downstream of persist (stats engine, dashboards, datapoint,
editor, review queue, RBAC) is **team-generic** and needs no change — the
`dialpad_call_id`/`dialpad_link` columns simply carry Retell values
(`call_…` ids fit the TEXT columns; renaming columns is not worth the blast
radius — treat them as "provider call id / provider review link").

## 2. Provider seam

```
src/lib/providers/
  types.ts     CallProvider interface + NormalizedCall
  dialpad.ts   extract of today's getTranscript/getCallDetails — ZERO behavior change
  retell.ts    new client (get-call v2; list-calls v3 later)
  index.ts     getProvider(teamConfig) → CallProvider
```

```ts
interface NormalizedCall {
  call_id: string;
  transcript_text: string;              // "Name: line" joined
  transcript_display: DisplayLine[];    // {timestamp, speaker, text}
  moments_display: any[];               // dialpad moments | retell signal markers (§4)
  caller_name: string;
  caller_phone: string;
  connected_at: string | null;          // ISO
  ended_at: string | null;
  duration_ms: number | null;
  entry_point_call_id: string | null;   // dialpad only
  master_call_id: string | null;        // dialpad only
  review_link: string;                  // dialpad callreview URL | retell public_log_url
  was_recorded: boolean;
  audio:                                // consumed by the workflow payload (§5)
    | { source: "dialpad" }             // workflow fetches via DIALPAD_API_KEY (today's path)
    | { source: "url"; url: string; mime: string };  // pre-signed, app-fetched-fresh
  grounding: {                          // replaces the CC-context call for retell (§6)
    context_block: string | null;       // text block for the judge/single-stage prompt
    sop_query: string | null;           // preferred SOP retrieval query when no disposition
    stamps: Record<string, unknown>;    // persisted into dialpad_call_metadata
  } | null;                             // null ⇒ dialpad path: keep fetchCallContext as-is
  agent_version: string | null;         // retell only — Sofia build stamp
}
```

Provider selection: `teams.provider` column (§3). `scoring.ts` swaps its
prefetch block for `provider.fetchCall(callId)`; the secret gate at `:170`
becomes provider-conditional (`dialpad` → `DIALPAD_API_KEY`, `retell` →
`RETELL_API_KEY`). CC grounding + disposition SOP query stay exactly as-is
for dialpad teams (`grounding === null` ⇒ current code path).

### Retell mapping (`retell.ts`, from RetellAPI.md §1)

GET `/v2/get-call/{call_id}`, `Authorization: Bearer RETELL_API_KEY`:

- `transcript_object[]` → `transcript_display` (speaker `agent` → "Sofia",
  `user` → caller, `transfer_target` labeled; timestamp = first word's
  `start` mm:ss) and `transcript_text`.
- `start/end_timestamp` (epoch ms) → ISO; `duration_ms` direct.
- `from_number`/`to_number` + `direction` → `caller_phone` (the human side);
  `caller_name` = collected dynamic variable when present, else "".
- `recording_multi_channel_url ?? recording_url` → `audio: {source:"url",
  url, mime:"audio/wav"}` — multi-channel preferred (clean speaker
  separation for the annotator).
- `public_log_url` → `review_link`.
- `agent_version` → `agent_version`.
- Guards: `call_status !== "ended"` → 422 "call not ended";
  `call_analysis.in_voicemail === true` → 422 "voicemail — not scoreable";
  422 from Retell → "call not found".

## 3. Config & schema — migration `0005_sofia_provider.sql`

```sql
ALTER TABLE teams ADD COLUMN provider TEXT NOT NULL DEFAULT 'dialpad'
  CHECK (provider IN ('dialpad','retell'));
ALTER TABLE teams ADD COLUMN provider_config TEXT
  CHECK (provider_config IS NULL OR json_valid(provider_config));

INSERT INTO teams (id, name, timezone, default_language, company, provider, provider_config)
VALUES ('sofia', 'Sofia AI', 'America/Los_Angeles', 'en', 'Landing Living LLC',
        'retell', json('{"agent_ids": ["<SOFIA_RETELL_AGENT_ID value>"]}'));

INSERT INTO qa_agents (team_id, name, canonical_name, email, supervisor_email, active)
VALUES ('sofia', 'Sofia', 'Sofia', 'sofia-ai@hellolanding.tech',
        'jackson.chretien@hellolanding.com', 1);  -- §9.3: Jackson owns review;
                                                  -- console auto-fills manager_email
```

- **Agent id lives in `provider_config`, not a Sandy secret** — it isn't
  sensitive, and `SOFIA_RETELL_AGENT_ID` is 21 chars (over Sandy's 20-char
  secret-name cap anyway). Array-shaped for future scratch/prod agent splits;
  used by list-calls filtering (R4) and optionally to warn when a scored
  call's `agent_id` isn't whitelisted.
- `qa_agents`: single synthetic "Sofia" row (roster check at `scoring.ts:177`
  requires it). Per-build trends come from `agent_version` stamps, not
  roster rows. `dialpad_agent_id` stays NULL.
- Rubric/formula seed: `sofia` v0 draft rubric (§7) in the same migration or
  a follow-up seed — the engine is config-driven, no code changes.
- `loadTeamConfig` gains `provider` + `provider_config` passthrough.

**Secrets:** `RETELL_API_KEY` = **app secret** (Dashboard-set; all Retell
calls originate app-side — the workflow only receives a pre-signed URL, so
no workflow-side Retell secret exists at all).

## 4. Retell-only signal exploitation (the point of this slice)

### 4.1 Verified conversational-dynamics markers (app-computed)

Dialpad gave us "moments" and the annotator's *observational* hold guesses.
Retell's `transcript_object[].words[].start/end` lets the **app compute
deterministic metrics** and feed them to the annotator as **system-verified**
markers (a tier Dialpad never had):

- response gaps: per agent-turn-after-user-turn latency (ms) — list + p50/p95
- overlaps/interruptions: turn starting before prior turn's last word ends
- silences > 3 s inside the call
- talk ratio (agent ms vs user ms)

These go into the existing `moments_display` slot (it already flows through
`buildAnnotatorPrompt` → payload → persist). The prompt header for that block
becomes provider-labeled: dialpad teams keep the current "Dialpad signal
markers" wording **byte-identical** (no score drift for MS/Sales); retell
teams get "PLATFORM SIGNAL MARKERS (system-verified)".

### 4.2 Latency percentiles → human-likeness evidence

`latency.e2e {p50,p90,p95,max}` (+ `asr`/`llm`/`tts` if present) appended to
the markers block: objective response-time stats (a human doesn't answer in
80 ms — nor in 4 s, every time, with zero variance). Also persisted in
`dialpad_call_metadata` for trend cuts.

### 4.3 `call_analysis` → grounding + SOP query

`grounding.context_block` (replaces the CC/disposition block in the judge +
single-stage prompts):

```
CALL PLATFORM ANALYSIS (Retell post-call, informational):
- Summary: {call_summary}
- Caller sentiment: {user_sentiment}   - Call successful: {call_successful}
- Disconnection: {disconnection_reason}
- Sofia build: agent_version {agent_version}
- Dynamic context given/collected: {metadata + retell_llm_dynamic_variables
  + collected_dynamic_variables, compact JSON}
```

`grounding.sop_query` = `call_analysis.call_summary` — `fetchSopContext`
gains an optional `summaryQuery` used when disposition is absent (better
than the current 600-char transcript-head fallback; sub-τ still falls into
the existing conservative `sop_context_missing` path).

**Tag scoping (added 2026-08-10, owner ask):** Sofia retrieves ONLY docs
tagged `Sofia`. `teams.retrieval_config` (migration 0006) carries a
provider-agnostic `{tags, match}` scope; Pulpo's `search_knowledge_base`
has no tag parameter, so the scope is enforced by filtering search hits
against the `list_documents_by_tag` id set (cached 1 h, case-insensitive,
≤50 docs). **Fail-closed**: unresolved scope ⇒ retrieval skipped
(`tag_scope_unavailable`), never a leak of other teams' docs. ⚠ At ship
time ZERO docs carried the `Sofia` tag — Sofia scoring runs the
conservative no-SOP path until docs are tagged in Pulpo. Future per-team
scoping (MS/Sales, non-Pulpo providers) = set the same config shape.

### 4.4 The rest

- `public_log_url` → review link on console/datapoint/editor (LLM req/resp +
  latency debug view — better than an audio player for diagnosing Sofia).
- `agent_version` → stamped into `dialpad_call_metadata` (+ shown on the
  datapoint): score trends **per Sofia build** while she's in testing.
- `disconnection_reason` → grounding context now; auto-skip filter in R4.
- list-calls v3 (R4): hourly pull = `call_status: ended` + `agent` filter +
  `start_timestamp` watermark → get-call each → enqueue. **Platform cap:
  2 cron schedules, both used ("7 * * * *" pump, "37 9 * * *" maintenance)
  — the pull rides the existing hourly "7 * * * *" handler in
  `index.tsx`/`maintenance.ts`, NOT a new schedule.**

## 5. Workflow change (`qa-scoring-pipeline` v0.3)

Payload gains `audio` (absent ⇒ `{source:"dialpad"}` — fully
backward-compatible; queued v0.2-shaped payloads still run):

- `source:"dialpad"` → today's meta+download block unchanged.
- `source:"url"` → skip Dialpad entirely; `fetch(url)` (WAV), then the
  identical Gemini upload → annotate path. No Retell key workflow-side.

Signed-URL expiry (24 h when `opt_in_signed_url`): payload is built at
enqueue; queue latency is seconds–minutes and stale-rescue re-queues cap at
~25 min — ample margin. If a URL ever does expire (job stuck > 24 h), the
download fails loudly and a console re-score rebuilds a fresh URL.

## 6. Scoring-path deltas (`scoring.ts`)

1. Prefetch block → `provider.fetchCall()`; secret gate provider-conditional.
2. `retell` teams: skip `fetchCallContext`; `callContextText` =
   `grounding.context_block`; `cc_stamps = null`;
   `dialpad_call_metadata` ⊇ `grounding.stamps` (latency, agent_version,
   call_analysis, disconnection_reason).
3. TEAM CONTEXT line gains a third branch:
   > TEAM CONTEXT: this call was answered by **Sofia, Landing's AI voice
   > agent, on the member support line**. Evaluate against the Sofia rubric:
   > SOP adherence and human-likeness. Sofia is not a human agent — do not
   > excuse errors on that basis, and score conversational naturalness
   > exactly as the rubric defines it.
   (MS/Sales strings stay byte-identical.)
4. `dialpad_link` → `review_link`; `dialpad_entry_point/master_call_id` →
   null for retell.
5. `emailDispatch.resolveGasUrl` → explicit map
   `{member_support: MS, sales: SALES}`; `sofia` (and any future team) →
   `undefined` → existing `'skipped'` receipt. ~~Email suppressed for Sofia
   by construction~~ **Superseded 2026-08-12 (owner):** Sofia has her own
   GAS deployment (secret `GAS_WEBAPP_URL_SOFIA`, exactly 20 chars; script
   `12vGwhCNK1…`); scorecard emails deliver to **Jackson** via the shared
   sender's optional `CONFIG.EMAIL.TO_OVERRIDE` (set in
   `qa-automation/teams/sofia/Branding.js` — no-op for MS/Sales; CC dropped
   when it duplicates To). Sofia's `Config.js` regenerates from
   `qa-automation/teams/sofia/team_config.json` via `build_config.py sofia
   --config …` — NOT from `backend/config/teams/` (Railway glob-loads that
   dir and must not learn about sofia).

## 7. Sofia rubric/formula v0 (draft — owner design pass required)

Engine is config-driven; this is seed data, not code. Proposed skeleton for
the owner conversation (grounded in "SOP + human-likeness"):

| # | Section | Type | Notes |
|---|---|---|---|
| 1 | SOP adherence | numeric 0–10 | Pulpo-retrieved SOP vs actual handling |
| 2 | Accuracy / no hallucinated policy | numeric 0–10 | audio SOT |
| 3 | Human-likeness: conversational flow | numeric 0–10 | uses §4.1 verified markers + latency |
| 4 | Human-likeness: tone & empathy | numeric 0–10 | audio-dependent |
| 5 | Intent capture & resolution | numeric 0–10 | vs call_analysis + transcript |
| 6 | Escalation / transfer handling | yn + NA | `transfer_target` turns, voicemail rules |

Formula: plain weighted sum, **`always_human_review: true`** while Sofia is
in testing — every eval parks in the review queue (same gate family as the
Sales `requiresAnalystReview` restoration, v0.26; explicit flag, not the
manual-section side-door). Relax once the rubric stabilizes.

Language: Sofia is EN-only today; audio-SOT rule applies unchanged if that
changes.

## 8. Rollout ladder (PRs, each deployable + verifiable)

- **R1 — seam extract (no behavior change):** `providers/` with `dialpad.ts`
  extraction; `scoring.ts` consumes `NormalizedCall`; `resolveGasUrl`
  explicit map; markers-block label parameterized (dialpad wording
  byte-identical). Gate: score 1 MS call E2E — identical scorecard/email.
- **R2 — Retell + Sofia live (manual console-first):** migration 0005 +
  rubric v0 seed; `retell.ts`; workflow v0.3 (`audio.url`); grounding block;
  SOP `summaryQuery`; TEAM CONTEXT branch; `KNOWN_TEAMS` + greeting card
  flip; console for `sofia` (single-agent roster auto-fill). User sets
  `RETELL_API_KEY` (Dashboard) + applies migration. Gate: score 1 real
  Sofia call → parks in review queue → editor renders → approve → NO email
  (receipt `skipped`).
- **R3 — Retell lookup variant** *(pulled forward 2026-08-10: call ids are
  not knowable a priori — discovery gates the whole pipeline)*: `/lookup/sofia`
  lists ended Sofia calls via list-calls v3 (agent-whitelisted, newest-first,
  paginated) with scored/queued status joins and one-click Score (reviewer =
  viewer's Access email). Plus rubric hardening with owner: section defs,
  weights, review-gate relaxation, per-build (`agent_version`) dashboard cut.
- **R4 — auto-pull + digest:** list-calls v3 hourly sweep on the existing
  "7 * * * *" cron + voicemail/short-call skip list; owner digest email
  (pushed from R3 — wants real data first, §9.5).

## 9. Open questions (owner/Max)

1. `provider_config.agent_ids`: seed with the one id from `.env`
   (`SOFIA_RETELL_AGENT_ID`) — are there scratch agents to exclude later?
   **Answered Aug/09** Seeded in the .env as 'SOFIA_RETELL_AGENT_ID'
2. `data_storage_setting` on Sofia's Retell agent: if
   `everything_except_pii`, scrubbed fields replace raw — verify her config
   stores transcript + recording before R2.
   **Answered Aug/09** Unknown, I have two API keys, only one in the .env, the other is readonly but if one fails we have the other.
3. `manager_email` for Sofia evals (console requires it): who owns review —
   Max? the MS QA manager?
   **Answered Aug/09** It should be 'jackson.chretien@hellolanding.com'
4. Rubric v0 sections/weights (§7) — owner sign-off before R2 seed.
  **Answered Aug/09**  v0 looks good!
5. Owner digest email for Sofia later, or dashboards-only stays the answer?
  **Answered Aug/09** Dashboards AND owner digest email once we have actual data.
