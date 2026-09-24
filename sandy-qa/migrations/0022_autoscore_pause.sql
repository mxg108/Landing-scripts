-- 0022 — AUTO-SCORING PAUSE (owner instruction 2026-09-24).
--
-- "Turn off all auto-scoring pipelines. Nothing moves until Diana or Todd
-- take over this project." — Max Pérez, 2026-09-24.
--
-- One switch, read by src/lib/pause.ts on every ticker step and hourly
-- tick: while the row exists, the nightly disposition sweep (Member
-- Support), the Retell sweep (Sofia), the one-shot cron jobs (EOM
-- assessments, HR-bonus dispatch, daily digest) do NOT run. The queue
-- pump, the EOD sheet report and the supervisor pulls keep working (they
-- do not score calls); console "Score Call" still works for a human.
--
-- TO RESUME (the new owner):   DELETE FROM qa_settings WHERE key = 'autoscore_paused';
--   (then check README.md "Paused" section for what to expect on the first
--    night back — the sweep only pulls YESTERDAY; older days are lost unless
--    re-armed, see references/CronContinuation.md §6.4).
CREATE TABLE IF NOT EXISTS qa_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT CHECK (value IS NULL OR json_valid(value)),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT INTO qa_settings (key, value) VALUES (
    'autoscore_paused',
    json_object(
        'since', '2026-09-24',
        'by', 'maximiliano.perez@hellolanding.com',
        'reason', 'Project on hold until Diana or Todd take over (owner instruction 2026-09-24). Delete this row to resume.'
    )
) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
