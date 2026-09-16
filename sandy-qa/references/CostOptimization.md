# Cost optimization — design (CO0)

Written 2026-09-16, one week after per-eval cost stamps went live (v0.72,
workflow v0.4). Owner ask: use prompt caching and batch processing "as
much as Google's platform allows" once the pipeline is running again
(it is — `CronContinuation.md`). Design-doc-first: nothing here is built
yet; §7 is the ladder.

## 0. Owner decisions needed (defaults proposed)

| # | Question | Proposed default |
|---|---|---|
| 1 | Latency tolerance for the **nightly** path (sweep at 06:07 UTC, results wanted by the MX morning shift ≈ 14:00 UTC) | Up to **6 h** is fine → batch-eligible. Console "Score Call" and rescore stay real-time. |
| 2 | Which budget hurts: **Gemini is billed to our key** (the "credits ran out" scare); **Claude is billed to Engineering's AI Gateway** (per-team attribution still open — sit-down item) | Optimize Gemini first for our budget; Claude levers still count for the company bill. |
| 3 | Quality bar for any model change | No model swap without the score-diff harness (30 calls, both models, same rubric) and an owner sign-off — same gate as the Claude-judge flip. |
| 4 | Gemini 2.5 Flash lifecycle | Official deprecations page (fetched 2026-09-16): **"No shutdown date announced"** for `gemini-2.5-flash`. The third-party "Oct 16" claim is not on Google's page. Newer Flash models cost 2.5–5× more (3.8 Flash $0.75/$3.75 until Dec 31, then $1.50/$7.50; 3.5 Flash $1.50/$9.00) — **do not migrate for cost reasons**; re-check monthly. |

## 1. Measured baseline (qa_evaluations cost stamps, 2026-09-08 → 09-16)

375 evals, $28.79 → **$0.077/eval blended, ≈ $3.20/day** at this week's
volume (which was depressed by the outage — a normal week is ≈ 50 MS +
20 Sofia + a few Sales per day ≈ $4.5–5/day ≈ **$140/month**).

Per-team profile (averages; tokens from `dialpad_call_metadata.sandy_pipeline`):

| | MS (n=212) | Sales (n=17) | Sofia (n=146) |
|---|---|---|---|
| call length | 10.6 min | 16 min | 2.3 min |
| Gemini annotate prompt | 22.2k (18.6k audio) | 37.6k (31.8k audio) | 6.2k (4.4k audio) |
| Gemini annotate output + thinking | 4.5k + 3.9k | 11.1k + 4.1k | 1.5k + 3.7k |
| Claude judge input (min) | 11.2k (6.0k) | 16.7k (16.3k) | 10.0k (5.5k) |
| Claude judge output | 3.9k | 5.2k | 4.2k |
| **cost/eval (stamped)** | **$0.087** | **$0.110** | **$0.059** |
| annotate / judge wall time | 66 s / 41 s | 75 s / 53 s | 48 s / 46 s |

Where an MS dollar goes (list prices: Gemini 2.5 Flash $0.30 text / $1.00
audio in, $2.50 out incl. thinking; Sonnet 5 $2 in / $10 out):

| Component | $/eval | share |
|---|---|---|
| Gemini audio input (18.6k × $1.00/M) | 0.019 | 21% |
| Gemini text input (3.6k × $0.30/M) | 0.001 | 1% |
| Gemini output + thinking (8.3k × $2.50/M) | 0.021 | 23% |
| **Gemini subtotal (our key)** | **0.041** | **45%** |
| Claude judge input (11.2k × $2/M) | 0.022 | 24% |
| Claude judge output (3.9k × $10/M) | 0.039 | 43% |
| **Claude subtotal (Engineering gateway)** | **0.061** | **67%** |

(Components sum to $0.10; the stamped average is $0.087 because some
evals ran shorter or single-stage. Shares are what matter.) The single
largest line is the **judge's output tokens** — the scorecard reasoning —
and the second is Gemini's audio input. Sofia's thinking tokens (3.7k on a
2-minute call) are out of proportion to its audio (4.4k).

## 2. Levers, ranked (MS $/eval, list prices; nightly path unless noted)

| # | Lever | Saves/eval | % | Effort | Quality risk | Notes |
|---|---|---|---|---|---|---|
| L1 | **Judge via Anthropic Message Batches** (nightly path) | 0.030 | 35% | M–H | none | 50% off all judge tokens; most batches finish < 1 h, max 24 h. **Blocker: does the Engineering AI Gateway proxy `/v1/messages/batches`?** Cloudflare's docs only show `/v1/messages`. Probe first (§4). |
| L2 | **Annotate via Gemini Batch API** (nightly path) | 0.020 | 23% | M–H | none | 50% off; JSONL requests may reference Files-API uploads (`fileData` URIs); target turnaround 24 h, "in the majority of cases much quicker"; supports `responseSchema` + caching. Our key. |
| L3 | **Judge prompt caching** (all paths) | 0.010 | 11% | L | none | Static prefix = system + rubric + team context ≈ 4–5k tokens (Sonnet 5 minimum 1024). Cache read 0.1×, write 1.25× (5-min TTL). The ticker pump keeps judge calls ~3 min apart overnight → one write per night, reads thereafter. Needs the user message as content blocks with a breakpoint after the rubric, and the estimator (`modelCosts.ts`) to price cache tiers. Verify with `usage.cache_read_input_tokens`. |
| L4 | **Annotator model A/B: `gemini-2.5-flash-lite`** ($0.30 audio in, $0.40 out) | up to 0.031 | 36% | L to run | **high** | Would cut Gemini from $0.041 to ≈ $0.009. Spanish audio is SOT ([[spanish-audio-sot]]) — the annotation carries emotion/pace/hold markers the judge depends on. Gate: 30-call score-diff + annotation fidelity spot check (5 calls, bilingual reviewer). |
| L5 | **Sofia thinking budget** 4096 → 1024 (Sofia only) | 0.007 (Sofia) | 12% of Sofia | L | low–med | 3.7k thinking on 2-min calls. Same score-diff gate on 30 Sofia calls. |
| L6 | **Annotator implicit caching** (reorder parts: text prompt before the audio part) | 0.001 | 1% | trivial | none | Gemini 2.5 implicit caching needs ≥ 2,048 identical leading tokens; our static annotator text ≈ 3.6k, but today the audio part comes FIRST so nothing ever matches. Free; do it with L2. Watch `usageMetadata.cachedContentTokenCount`. |
| L7 | Judge output trim (terser reasoning) | 0.012–0.015 | 15% | L | **med** | The reasoning IS the coaching product; only with the QA team's sign-off on a shorter scorecard format. Parked. |
| — | Judge model downgrade (Haiku 4.5) | 0.030 | 35% | L | **high** | Not proposed — the Sonnet judge was chosen on a score-diff study; a downgrade is the owner's call, and only through the same study. |

Stacking the safe ones on the nightly path: L3 + L6 ≈ −12% now; L1 + L2
≈ −58% on top. **From $0.087 → ≈ $0.035/eval on nightly MS**; Gemini
(our key) from $0.041 → ≈ $0.020 (L2) or ≈ $0.009 (L4 if it passes).

Honest dollar view at today's volume: ≈ $140/month → ≈ $60/month.
The design is worth more as volume grows (Sales nightly sweep, per_agent
> 3, Sofia at 20+/day) than at this week's run-rate — which is why the
ladder puts the free levers first and the pipeline restructure second.

## 3. Nightly batch pipeline (L1 + L2) — shape

Today: sweep enqueues 50 jobs → the queue runs **one** `qa-scoring-pipeline`
run at a time (platform slot) → each run = download → Gemini upload →
annotate → judge → callback (≈ 2.5–3 min) → ≈ 2.5 h to drain.

Proposed (nightly path only; the real-time path is untouched):

```
qa-cron-ticker step                   qa-scoring-batch workflow (new)
──────────────────                    ───────────────────────────────
sweep enqueue (as today, but          1. for each job: Dialpad download → Gemini Files upload
 rows marked lane='batch')               (parallel ×5; the Files API is the only per-call I/O left)
      │                               2. submit ONE Gemini batch (JSONL, fileData URIs,
      ▼                                  responseSchema, thinkingConfig) → batch name
drain: when ≥ N batch rows are        3. poll every 60 s (step.sleep) until done
 queued and no batch run is           4. parse annotations → build judge prompts app-side
 active → trigger the batch run          (callback "annotations-ready" → app renders prompts
 with the job ids                        from D1 rubric/SOP exactly as today → returns them)
      │                               5. submit ONE Anthropic batch (custom_id = job_id,
      ▼                                  cache_control on the static prefix) → poll
per-job callbacks persist evals       6. per-job callback (same payload shape as today)
 exactly as today                        + Gemini file cleanup
```

Design points:
- **One batch run per night** (platform: one active run per workflow) — a
  second workflow name (`qa-scoring-batch`) keeps its own slot, so console
  scoring still flows through `qa-scoring-pipeline` in parallel.
- Prompt building stays app-side (byte-stable prompts, SOP retrieval at
  trigger time — unchanged doctrine); the workflow only moves tokens.
- Failure handling: a batch item `errored`/`expired` → that job requeues
  on the real-time lane (attempts++), so a batch hiccup costs full price
  for the stragglers, never a missing eval.
- Cost stamps: batch responses carry `usage`/`usageMetadata` like today;
  `estimateEvalCost` gains a `batch: true` × 0.5 factor and cache tiers.
- Timing: upload 50 files ≈ 3 min, Gemini batch typically < 30 min,
  Anthropic batch typically < 1 h → evals land by ≈ 08:30 UTC instead of
  ≈ 09:30 today. Worst case (24 h) is still inside decision #1 only if
  the owner accepts "next day"; otherwise a 6 h deadline → fall back to
  the real-time lane for whatever is still pending.

## 4. Probes before building (cheap, each ≤ 1 h)

1. **Gateway batches probe** — from a throwaway workflow step:
   `GET https://gateway.ai.cloudflare.com/v1/<acct>/sandy-workflows/anthropic/v1/messages/batches`
   with `cf-aig-authorization`. 200 → L1 is buildable through the gateway;
   404/405 → ask Engineering for either a gateway route or a direct
   Anthropic key scoped to QA (the same ask as per-team cost attribution).
2. **Gateway `cache_control` passthrough** — one judge call with a
   breakpoint; check `usage.cache_creation_input_tokens > 0`, then a second
   call within 5 min shows `cache_read_input_tokens > 0`.
3. **Gemini Batch + Files** — one batch of 2 requests referencing uploaded
   audio; confirm `responseSchema` + `thinkingConfig` are honored and the
   `usageMetadata` modality split survives (our cost stamp needs it).
4. **Flash-Lite A/B** (L4) — 30 MS calls annotated by both models, judged
   by the same Sonnet prompt; compare overall_score deltas and the
   annotation fidelity spot check. Cost of the probe ≈ $3.

## 5. Guardrails the owner asked for implicitly ("credits ran out")

- **Budget alerts**: set a Google Cloud budget on the Gemini project (e.g.
  $100/month, alerts at 50/90%) and note the account owner in
  `.env`/1Password; the Sep 13–16 outage looked like a quota event and
  was not — an alert would have settled it in a minute.
- **Cost dashboard**: `qa_evaluations.estimated_cost_usd` is stamped since
  Sep 8; a daily sum by team (already a 1-line query) belongs on the
  admin page — ladder item CO2.
- **Spend cap in the pipeline**: `max_enqueues` (120/night) already bounds
  the nightly spend to ≈ $10; keep it.

## 6. Non-goals

- Changing the rubric, formula, or scorecard content for cost reasons.
- Migrating the annotator off Gemini 2.5 Flash before Google announces a
  date (decision #4).
- Batch for the console/real-time path.

## 7. Ladder CO0–CO5

- **CO0** this doc + decisions §0 (owner).
- **CO1** L3 + L6 (free): content-block judge prompt with `cache_control`
  after the rubric; annotator parts reorder; `modelCosts.ts` cache tiers;
  workflow v0.5. Gate: stamps show `cache_read_input_tokens > 0` on night 2
  and `cachedContentTokenCount > 0`; cost/eval −10% on MS.
- **CO2** cost line on the admin page (daily $ by team, 30 days) + the
  Google Cloud budget alert. Gate: the owner can answer "what did QA cost
  this week" from the app.
- **CO3** probes §4.1–4.3 → decide L1/L2 transport. Gate: written
  answers in this doc.
- **CO4** `qa-scoring-batch` workflow + batch lane (§3). Gate: one
  supervised night, 0 missing evals, stamps ≈ −55% on the nightly lane,
  stragglers visible in `qa_score_queue.last_error`.
- **CO5** L4 / L5 experiments via the score-diff harness; owner sign-off
  per model change.
