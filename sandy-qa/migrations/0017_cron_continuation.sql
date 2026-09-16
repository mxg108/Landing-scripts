-- 0017 — cron continuation (CronContinuation.md §3).
--
-- The nightly disposition sweep becomes a phase state machine driven by
-- the qa-cron-ticker workflow (bounded steps, exact resume) instead of one
-- multi-minute cron tick — the Sandy scheduler bounds dispatch time since
-- 2026-09-14 and cut every tick that carried real work.
--
-- status keeps its CHECK set ('pending','fetching','completed','error');
-- phase refines 'fetching': initiate → poll → fill → select → enqueue → done.
ALTER TABLE qa_disposition_pulls ADD COLUMN phase TEXT;
ALTER TABLE qa_disposition_pulls ADD COLUMN cursor INTEGER NOT NULL DEFAULT 0;
-- parsed + deduped export rows between poll and select (cleared at select)
ALTER TABLE qa_disposition_pulls ADD COLUMN export_json TEXT
    CHECK (export_json IS NULL OR json_valid(export_json));
-- selected calls between select and completed
ALTER TABLE qa_disposition_pulls ADD COLUMN picks TEXT
    CHECK (picks IS NULL OR json_valid(picks));
-- expired-export re-initiations on this row (cap 3 → error)
ALTER TABLE qa_disposition_pulls ADD COLUMN reinits INTEGER NOT NULL DEFAULT 0;
