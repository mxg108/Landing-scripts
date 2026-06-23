-- ============================================================================
-- Migration 007 — DOWN
--
-- Drops in reverse FK-dependency order. No CASCADE — if a downstream
-- consumer (e.g. qa.evaluations.sop_used_document_id with an FK added in
-- a future migration) is referencing embeddings.sop_documents, the
-- drop will fail loudly. As of v1.1 that FK does NOT exist (006 ships
-- the column without REFERENCES), so down here is straightforward.
-- ============================================================================

DROP TABLE IF EXISTS embeddings.embedding_runs;
DROP TABLE IF EXISTS embeddings.sop_chunk_embeddings;
DROP TABLE IF EXISTS embeddings.sop_chunks;
DROP TABLE IF EXISTS embeddings.sop_documents;
DROP TABLE IF EXISTS embeddings.embedding_models;
