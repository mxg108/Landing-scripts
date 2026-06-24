-- ============================================================================
-- Migration 005: command_center.* tables
-- Spec: database/SQLMigration.md §4
--
-- Six tables that make CC's runtime state durable across Railway redeploys:
--
--   webhook_events           Append-only log; truth for state derivation
--                            (§4.1). Replay rebuilds in-memory state via
--                            the same handler used live.
--   calls                    Per-call materialized aggregate; load-bearing
--                            FK target for qa.evaluations and the
--                            calls-received-vs-scored ratio (§4.2).
--   chiclets                 Stable per-surfaced-chiclet identity (§4.3).
--   chiclet_events           SSE emission log (§4.4).
--   frequent_callers_cache   Per-team Looker snapshot (§4.5).
--   dialpad_agents           Dialpad agent-id ↔ display-name registry (§4.6).
--
-- Sequenced before 006 because qa.evaluations.command_center_call_id is a
-- nullable FK → command_center.calls.id (§3.4 + §4.2 + §7.6).
-- Hold-intervals are NOT modeled (§4.7) — derived from webhook_events when
-- needed; total_hold_seconds rollup on `calls` covers the 95% case.
-- The dormant call_state_snapshots DDL in §4.1.3 is intentionally NOT
-- applied here — reserved for the day the perf tripwire fires.
-- ============================================================================


-- ── command_center.webhook_events (§4.1) ────────────────────────────────────
-- Discriminated by event_kind: call events and agent-status events share the
-- log but each gets its own dedupe key (see partial UNIQUEs below).
--
-- event_timestamp is from the Dialpad payload (event time), NOT received_at
-- (our clock) — replay-order correctness depends on this distinction.

CREATE TABLE command_center.webhook_events (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    received_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    team_id                     TEXT NOT NULL REFERENCES public.teams(id),
    event_kind                  TEXT NOT NULL,
    dialpad_call_id             TEXT,
    dialpad_master_call_id      TEXT,
    dialpad_agent_id            TEXT,
    state                       TEXT NOT NULL,
    event_timestamp             TIMESTAMPTZ NOT NULL,
    raw_payload                 JSONB NOT NULL,
    processed_at                TIMESTAMPTZ,
    CONSTRAINT webhook_events_event_kind_check
        CHECK (event_kind IN ('call', 'agent_status')),
    -- Call events MUST carry a dialpad_call_id (agent-status events may not).
    CONSTRAINT webhook_events_call_id_when_call
        CHECK (event_kind <> 'call' OR dialpad_call_id IS NOT NULL),
    -- Per §4.1.1, monitored_states are:
    --   ringing, connected, hold, hangup, recording, call_transcription,
    --   recap_summary
    -- Agent-status events use a separate vocabulary defined by Dialpad's
    -- agent-status webhook (available / busy / etc.). We keep `state` as
    -- TEXT NOT NULL but do not constrain its values here — a CHECK that
    -- ages with Dialpad's vocabulary would be fragile.
    CONSTRAINT webhook_events_state_not_blank CHECK (length(state) > 0)
);

-- Partial UNIQUE indexes per §4.1: event-kind-aware dedupe so an
-- accidental replay of the same call-event payload is a no-op, and an
-- agent-status event with the same timestamp as an unrelated call event
-- on the same id doesn't collide.
CREATE UNIQUE INDEX uq_webhook_call ON command_center.webhook_events
    (dialpad_call_id, state, event_timestamp)
    WHERE event_kind = 'call';

CREATE UNIQUE INDEX uq_webhook_agent ON command_center.webhook_events
    (dialpad_agent_id, state, event_timestamp)
    WHERE event_kind = 'agent_status';


-- ── command_center.calls (§4.2) ─────────────────────────────────────────────
-- Per-call materialized aggregate. Universe of every call CC has observed
-- (scored or not). Written-through by the same handler that processes
-- webhook_events — live and replay are the same function (§4.1.2 invariant).
--
-- `scored` is monotonic — never flips back to FALSE even if the
-- corresponding evaluation is later deleted. Deletion flips
-- `evaluation_orphaned` instead (§3.9, §4.2).

CREATE TABLE command_center.calls (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id                     TEXT NOT NULL REFERENCES public.teams(id),
    dialpad_call_id             TEXT NOT NULL,
    dialpad_master_call_id      TEXT,
    dialpad_entry_point_call_id TEXT,
    -- Call lifecycle clocks from Dialpad's get_call_details(). connected_at
    -- is the call-time source of truth (per CallTimeOnAnalystHistory.md).
    started_at                  TIMESTAMPTZ,
    rang_at                     TIMESTAMPTZ,
    connected_at                TIMESTAMPTZ,
    ended_at                    TIMESTAMPTZ,
    total_duration_ms           BIGINT,
    direction                   TEXT,
    external_number             TEXT,
    internal_number             TEXT,
    group_id                    TEXT,
    -- Denormalized agent fields for fast queries. The dialpad_agents
    -- registry (§4.6) is the source of truth for the id ↔ name mapping.
    dialpad_agent_id            TEXT,
    agent_name                  TEXT,
    caller_name                 TEXT,
    caller_phone_e164           TEXT,
    caller_email                TEXT,
    target_name                 TEXT,
    target_type                 TEXT,
    target_phone                TEXT,
    mos_score                   NUMERIC(3,2),
    was_recorded                BOOLEAN,
    is_transferred              BOOLEAN,
    -- Normalized recording-URL shape per §3.4.2: {audio: [...], screen: [...]}
    recording_urls              JSONB,
    last_state                  TEXT,
    last_state_at               TIMESTAMPTZ,
    -- Accumulated across hold cycles; derived from webhook_events on each
    -- write. NOT NULL DEFAULT 0 so rollup queries never have to coalesce.
    total_hold_seconds          INTEGER NOT NULL DEFAULT 0,
    -- Forward-compat catch-all. Per §4.2, planned post-v1 retention nulls
    -- this column past 2 years; first-class columns persist forever.
    raw_call_details            JSONB,
    -- Load-bearing scored/orphaned flags per §4.2 + §3.9. Monotonic.
    scored                      BOOLEAN NOT NULL DEFAULT FALSE,
    scored_at                   TIMESTAMPTZ,
    evaluation_orphaned         BOOLEAN NOT NULL DEFAULT FALSE,
    evaluation_orphaned_at      TIMESTAMPTZ,
    -- Provenance of CC's awareness of this call:
    --   webhook        CC saw it live via Dialpad webhook
    --   qa_on_demand   QA scored a call CC never saw live (UPSERT path 2)
    --   qa_backfill    Phase-B stub created from Analyst_History (path 3)
    seen_via                    TEXT NOT NULL,
    first_seen_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT calls_seen_via_check
        CHECK (seen_via IN ('webhook', 'qa_on_demand', 'qa_backfill')),
    -- scored timestamp paired with the flag: either both NULL or both set.
    CONSTRAINT calls_scored_pair_check
        CHECK (scored = FALSE OR scored_at IS NOT NULL),
    -- Same pair invariant for the orphan flag.
    CONSTRAINT calls_orphaned_pair_check
        CHECK (evaluation_orphaned = FALSE OR evaluation_orphaned_at IS NOT NULL),
    -- §4.2 explicit UNIQUE constraint — names the constraint so the
    -- ON CONFLICT target is stable.
    CONSTRAINT uq_calls_team_call_id UNIQUE (team_id, dialpad_call_id)
);


-- ── command_center.chiclets (§4.3) ──────────────────────────────────────────
-- Stable identity per surfaced chiclet. Permanent retention; `data` JSONB
-- carries per-type live fields written-through on every chiclet_updated SSE
-- emit (CC P2.2 — snapshot endpoint becomes a single indexed read).

CREATE TABLE command_center.chiclets (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id           TEXT NOT NULL REFERENCES public.teams(id),
    type              TEXT NOT NULL,
    tier              TEXT NOT NULL,
    status            TEXT NOT NULL,
    border_state      TEXT,
    source_event_id   BIGINT REFERENCES command_center.webhook_events(id),
    caller_phone_e164 TEXT,
    agent_name        TEXT,
    summary           TEXT NOT NULL,
    data              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ,
    resolved_by       TEXT,
    CONSTRAINT chiclets_type_check
        CHECK (type IN ('hold', 'repeated', 'frequent', 'qa_outlier',
                        'sheets_update', 'mass_notif', 'profanity')),
    CONSTRAINT chiclets_tier_check
        CHECK (tier IN ('T1', 'T2', 'T3')),
    CONSTRAINT chiclets_status_check
        CHECK (status IN ('active', 'resolved')),
    -- resolved pair: resolved status MUST carry resolved_at + resolved_by.
    CONSTRAINT chiclets_resolved_pair_check
        CHECK (status <> 'resolved'
               OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL))
);


-- ── command_center.chiclet_events (§4.4) ────────────────────────────────────
-- Append-only SSE-emission log. 30-day rolling retention (pruning is a
-- cron concern, not a schema concern).

CREATE TABLE command_center.chiclet_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chiclet_id  BIGINT NOT NULL REFERENCES command_center.chiclets(id)
                ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    emitted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chiclet_events_type_check
        CHECK (event_type IN ('created', 'updated', 'escalated', 'resolved'))
);


-- ── command_center.frequent_callers_cache (§4.5) ────────────────────────────
-- Per-team Looker registry snapshot. Two-snapshot retention; trimming runs
-- via cron.

CREATE TABLE command_center.frequent_callers_cache (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id           TEXT NOT NULL REFERENCES public.teams(id),
    caller_phone_e164 TEXT NOT NULL,
    caller_name       TEXT,
    unit              TEXT,
    category          TEXT,
    flag_reason       TEXT,
    last_call_at      TIMESTAMPTZ,
    total_calls_30d   INTEGER,
    snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    snapshot_id       BIGINT NOT NULL
);


-- ── command_center.dialpad_agents (§4.6) ────────────────────────────────────
-- Per-team Dialpad agent-id → display-name map. Drives both CC's live
-- agent-name resolution and the nightly reconciliation sweep
-- (qa.agents.dialpad_agent_id backfill per §3.11).

CREATE TABLE command_center.dialpad_agents (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id           TEXT NOT NULL REFERENCES public.teams(id),
    dialpad_agent_id  TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    email             TEXT,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_dialpad_agents_team_agent UNIQUE (team_id, dialpad_agent_id)
);
