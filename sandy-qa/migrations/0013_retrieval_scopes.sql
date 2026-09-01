-- 0013_retrieval_scopes.sql — Sofia-doc leak fix (score-diff analysis,
-- 2026-08-31). Pulpo's tag taxonomy went namespaced: Sofia's
-- machine-maintained engineering estate (subagent specs, tool specs,
-- flows) carries system:sofia and was outranking human SOPs in unscoped
-- MS/Sales retrieval (45–64% of Aug evals injected Sofia docs; −5.8 pts
-- within the nightly cohort). Scope shape gains exclude_tags, enforced on
-- the tags each search hit carries (sopRetrieval.applyTagScope — replaces
-- the list_documents_by_tag roundtrip and its 50-doc truncation).
--
-- sofia: allow-scope widens to system:sofia — her actual SOP suite
-- ("Sofia SOP — *") lives there; the bare sofia tag alone never matched it.
-- member_support/sales: shared pool minus system:sofia. Bare sofia-tagged
-- docs stay retrievable for MS/Sales by owner decision (2026-08-31): many
-- are shared member docs, and MS has no dedicated SOP corpus yet.

UPDATE teams SET retrieval_config = '{"tags":["sofia","system:sofia"],"match":"any"}'
WHERE id = 'sofia';

UPDATE teams SET retrieval_config = '{"exclude_tags":["system:sofia"]}'
WHERE id IN ('member_support','sales');
