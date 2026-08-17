-- 0006_retrieval_config.sql — per-team doc-retrieval tag scope
-- (SofiaRetellSpec §4.3 addendum, 2026-08-10). Provider-agnostic shape:
-- {tags: [...], match: 'any'|'all'} — enforced today by filtering Pulpo
-- search hits against list_documents_by_tag ids (search_knowledge_base has
-- no tag param); a future non-Pulpo RAG provider scopes off the same config.
-- NULL = unscoped (member_support/sales keep current behavior).

ALTER TABLE teams ADD COLUMN retrieval_config TEXT
    CHECK (retrieval_config IS NULL OR json_valid(retrieval_config));

UPDATE teams SET retrieval_config = '{"tags":["Sofia"],"match":"any"}'
WHERE id = 'sofia';
