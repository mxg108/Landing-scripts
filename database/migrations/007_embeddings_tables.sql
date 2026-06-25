-- ============================================================================
-- Migration 007: embeddings.* tables
-- Spec: database/SQLMigration.md §5
--
-- Model-agnostic, multi-language RAG groundwork for LandGPT v2. The current
-- pipeline does NOT query this schema — sizing and benchmark cadence
-- (§5.8) follow the LandGPT v2 timeline.
--
-- Hard constraint: pgvector's ivfflat/HNSW indexes top out at 2,000
-- dimensions. The column set covers VECTOR(1536) and VECTOR(1024); both
-- are addable to HNSW indexes (008). A VECTOR(3072) column is storable
-- but unindexable — use Matryoshka truncation to ≤ 2000 instead.
--
-- The "exactly one embedding_* column non-NULL, matching the model's
-- declared dimensions" invariant is partially expressed: the CHECK below
-- enforces the "exactly one non-NULL" half. The dim-match half is
-- application-layer (the writer joins embedding_models to choose which
-- column to populate); modeling it as a TRIGGER would couple the schema
-- to embedding_models update behavior in a way that's not worth the
-- complexity until LandGPT v2 ships.
-- ============================================================================


-- ── embeddings.embedding_models (§5.3) ──────────────────────────────────────
-- Registry of embedding models the indexer knows how to use. Multiple
-- models can be `is_current` simultaneously when they target different
-- modalities (text vs text+image), but at most ONE per modality (partial
-- UNIQUE index below).

CREATE TABLE embeddings.embedding_models (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name              TEXT NOT NULL,
    version           TEXT NOT NULL,
    dimensions        INTEGER NOT NULL,
    modality          TEXT NOT NULL,
    is_cross_lingual  BOOLEAN NOT NULL,
    provider          TEXT NOT NULL,
    is_open_source    BOOLEAN NOT NULL,
    cpu_deployable    BOOLEAN NOT NULL,
    is_current        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT embedding_models_dimensions_check
        CHECK (dimensions > 0 AND dimensions <= 2000),
    CONSTRAINT embedding_models_modality_check
        CHECK (modality IN ('text', 'text+image', 'text+audio')),
    CONSTRAINT embedding_models_provider_check
        CHECK (provider IN ('google', 'bge', 'qwen-team', 'local')),
    -- §5.3: "Only cpu_deployable=TRUE may be is_current."
    CONSTRAINT embedding_models_current_requires_cpu_deployable
        CHECK (is_current = FALSE OR cpu_deployable = TRUE),
    CONSTRAINT uq_embedding_models_name_version UNIQUE (name, version)
);

-- §5.3: "At most one TRUE per modality (partial UNIQUE)." Enforced via a
-- partial UNIQUE index that only sees `is_current=TRUE` rows.
CREATE UNIQUE INDEX uq_embedding_models_current_per_modality
    ON embeddings.embedding_models (modality)
    WHERE is_current = TRUE;


-- ── embeddings.sop_documents (§5.4) ─────────────────────────────────────────
-- Per-team SOP corpus. UNIQUE (team_id, version_tag) makes successive
-- versions of the same SOP coexist. The partial UNIQUE on is_current per
-- (team_id, language) makes "what's the live SOP for this team and
-- language" a single indexed read.

CREATE TABLE embeddings.sop_documents (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id       TEXT NOT NULL REFERENCES public.teams(id),
    title         TEXT NOT NULL,
    version_tag   TEXT NOT NULL,
    language      TEXT NOT NULL,
    source_url    TEXT,
    published_at  TIMESTAMPTZ,
    is_current    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_sop_documents_team_version UNIQUE (team_id, version_tag)
);

CREATE UNIQUE INDEX uq_sop_documents_current_per_team_language
    ON embeddings.sop_documents (team_id, language)
    WHERE is_current = TRUE;


-- ── embeddings.sop_chunks (§5.5) ────────────────────────────────────────────
-- Vector-agnostic chunk row. Same chunk can be embedded with multiple
-- models in parallel; embeddings live in sop_chunk_embeddings, not here.

CREATE TABLE embeddings.sop_chunks (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id       BIGINT NOT NULL REFERENCES embeddings.sop_documents(id)
                      ON DELETE CASCADE,
    chunk_index       INTEGER NOT NULL,
    text              TEXT NOT NULL,
    token_count       INTEGER,
    language          TEXT,
    modality          TEXT,
    -- Nested heading context (e.g. ["Procedures", "Move-In", "Step 3"])
    -- — drives retrieval-time context rendering.
    heading_path      TEXT[],
    page_number       INTEGER,
    image_asset_url   TEXT,
    CONSTRAINT uq_sop_chunks_doc_index UNIQUE (document_id, chunk_index)
);


-- ── embeddings.sop_chunk_embeddings (§5.6) ──────────────────────────────────
-- Per-(chunk, model) vector. The dim-class column set covers 1536 and
-- 1024 today; new dim classes ship as new columns (1280, etc.) when
-- benchmarks demand. HNSW indexes per non-NULL column live in 008.

CREATE TABLE embeddings.sop_chunk_embeddings (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_id        BIGINT NOT NULL REFERENCES embeddings.sop_chunks(id)
                    ON DELETE CASCADE,
    model_id        BIGINT NOT NULL REFERENCES embeddings.embedding_models(id),
    embedding_1536  VECTOR(1536),
    embedding_1024  VECTOR(1024),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- §5.6: "exactly one embedding_* non-NULL". The dim-match half (i.e.
    -- if model_id's dimensions=1024 then embedding_1024 is the populated
    -- one) is application-layer (see header comment).
    CONSTRAINT chunk_embeddings_exactly_one_dim CHECK (
        (embedding_1536 IS NOT NULL AND embedding_1024 IS NULL)
        OR (embedding_1024 IS NOT NULL AND embedding_1536 IS NULL)
    ),
    CONSTRAINT uq_chunk_embeddings_chunk_model UNIQUE (chunk_id, model_id)
);


-- ── embeddings.embedding_runs (§5.7) ────────────────────────────────────────
-- Batch run audit log. One row per (document × model) indexing pass.

CREATE TABLE embeddings.embedding_runs (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id        BIGINT NOT NULL REFERENCES embeddings.sop_documents(id)
                       ON DELETE CASCADE,
    model_id           BIGINT NOT NULL REFERENCES embeddings.embedding_models(id),
    chunk_count        INTEGER,
    total_tokens       INTEGER,
    estimated_cost_usd NUMERIC(10,4),
    started_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'running',
    error_detail       TEXT,
    CONSTRAINT embedding_runs_status_check
        CHECK (status IN ('running', 'completed', 'failed', 'aborted'))
);
