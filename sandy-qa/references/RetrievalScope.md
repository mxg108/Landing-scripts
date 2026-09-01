# RetrievalScope — per-team Pulpo tag scoping v2 (hit-tag enforcement)

*2026-08-31. Supersedes the tag-scope mechanism in SofiaRetellSpec §4.3
(the `list_documents_by_tag` doc-id roundtrip). Companion analysis in the
session that produced migration 0013; summary preserved here.*

## Why (the August score-diff investigation)

MS Overall fell 79.8 → 57.8 across the Railway→Sandy transition. The
decomposition (D1 evidence, 2026-08-31):

1. **Judge model swap** — Railway text judge `gemini-2.5-flash`; Sandy
   two-stage judge `claude-sonnet-5` (same formula/rubric). Largest factor.
2. **Sofia SOP leakage (this doc's subject)** — Sofia's machine-maintained
   engineering estate (`system:sofia`: subagent specs, tool specs, flows)
   landed in Pulpo from ~2026-07-31 and outranked human SOPs in unscoped
   MS/Sales retrieval. 45% of nightly-cohort MS evals injected Sofia docs;
   contaminated evals ran **−5.8 pts** vs clean within the same cohort.
   It equally polluted late-Railway evals (Aug: 64% contaminated, −4.3).
   Worst live example: `"Unit Issues — Maintenance request"` returned
   *only* Sofia docs above τ (top: `Sofia Tool — Create Maintenance
   Request`, 0.730).
3. **Selection change** — nightly random sweep vs analyst-picked calls.

## Mechanism

`teams.retrieval_config` (provider-agnostic), enforced by
`sopRetrieval.applyTagScope` on the **tags each search hit carries**
(Pulpo returns canonicalized tags on every hit — verified live). The old
`list_documents_by_tag` id-set roundtrip is gone: it cost an extra tool
call per scope and silently truncated at 50 docs (`system:sofia` already
exceeds that).

| Team | retrieval_config (migration 0013) | Meaning |
|---|---|---|
| sofia | `{"tags":["sofia","system:sofia"],"match":"any"}` | allow-scope: only her doc families. Widened to `system:sofia` — her actual SOP suite ("Sofia SOP — *") lives there; bare `sofia` alone never matched it |
| member_support | `{"exclude_tags":["system:sofia"]}` | shared pool minus Sofia's engineering estate |
| sales | `{"exclude_tags":["system:sofia"]}` | same |

Rules (see `tests/sop_scope.test.mjs`):

- `exclude_tags` beats everything: a hit carrying any excluded tag drops.
- `tags` + `match any|all` allow-scopes; both may combine.
- **Fail-closed**: under an active scope, a hit with *no tags array*
  (unknown tag state — an API regression) drops rather than leak.
- Scope filtering is post-search-cache (cache stays scope-agnostic).
- Empty post-filter result → `skipped_reason: "no_hits_in_team_scope"` →
  the standard conservative `sop_context_missing` path.

**Owner decision (2026-08-31):** bare `sofia`-tagged docs stay retrievable
for MS/Sales — many are shared member docs (`General Check-In Procedures`,
`Verification Information`) and MS has no dedicated SOP corpus yet. The
long-term fix is tag hygiene in Pulpo (shared docs shouldn't carry a bare
team tag) + dedicated MS SOPs; revisit the exclude set then.

## Provenance + flowchart rendering

- `pulpo_docs` provenance entries now stamp `tags` + `body_format` —
  contamination queries become exact SQL instead of title-LIKE forensics.
- `renderSopBlock` prepends a reading note to `body_format:"flowchart"`
  docs (raw Mermaid: follow nodes/edges; `%% def` lines carry node rules;
  `%% always/never` are doc-wide guardrails). Required by the sofia scope
  widening — her SOP suite is largely flowchart-form.

## human_review_required NA-lock (same PR)

The approve path NA-locks manual sections whose formula section declares
`na_default` (MS `human_review_required`, weight 10): Railway never
persisted a value there (Stage-1 NA + NA-spread redistribution); the Sandy
editor let manual numerics through on 9 evals, silently shifting overalls.
Editor-sent values for locked sections are coerced back to NA
(`score_source='manual_default'`). Sales's manual sections (no
`na_default`) stay analyst-editable.

## Verification

- `node tests/sop_scope.test.mjs` — 12 cases, stubbed MCP endpoint.
- sqlite-validated migrations 0001→0013.
- Live gate: after the next nightly sweep, assert zero `system:sofia`
  tags in new MS provenance:
  `SELECT COUNT(*) FROM qa_evaluations WHERE id>=10000000 AND
  created_at>='<deploy date>' AND dialpad_call_metadata LIKE '%system:sofia%'`
  (expect 0 for member_support/sales; sofia evals SHOULD carry them).
