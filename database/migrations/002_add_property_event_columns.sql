-- ============================================================================
-- Migration 002: Promote property_name/event_name to top-level columns,
--                fix indexes to match actual query patterns, and tighten
--                nullable columns on the recipients table.
--
-- Context:
--   The four query functions in Database.gs (fetchByDate, fetchByRecipient,
--   fetchByProperty, fetchByActor) all use expressions that make the
--   original B-tree indexes from 001 unusable:
--     - created_at::date cast defeats idx_campaigns_created_at
--     - LOWER(email) defeats idx_recipients_email
--     - LOWER() + LIKE on actor/propertyName need expression indexes
--
--   Additionally, propertyName and eventName are extracted from JSONB in
--   3 of 4 queries. Promoting them to real columns avoids repeated JSONB
--   extraction and allows direct indexing.
--
-- Safe to re-run: all statements use IF NOT EXISTS checks.
-- ============================================================================


-- 1. New columns on campaigns ------------------------------------------------
-- Nullable because existing rows won't have them populated until the
-- backfill below runs, and because some campaigns may genuinely lack
-- a property or event (e.g., UNDO operations).

ALTER TABLE mass_notifications.campaigns
    ADD COLUMN IF NOT EXISTS property_name TEXT,
    ADD COLUMN IF NOT EXISTS event_name    TEXT;


-- 2. Backfill from existing JSONB data ---------------------------------------
-- Pulls propertyName and eventName out of config_snapshot for every row
-- that has a non-null snapshot. Idempotent -- running twice is harmless
-- because the second run just overwrites with the same values.

UPDATE mass_notifications.campaigns
SET property_name = config_snapshot->>'propertyName',
    event_name    = config_snapshot->>'eventName'
WHERE config_snapshot IS NOT NULL;


-- 3. New columns / defaults on recipients ------------------------------------
-- created_at: lets us track when the recipient row was inserted, not just
--             when the parent campaign was created.

ALTER TABLE mass_notifications.recipients
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();


-- 4. Fix existing NULL values before NOT NULL constraints --------------------
-- Must run BEFORE setting NOT NULL so existing rows don't violate the
-- constraint. If no NULLs exist, these are harmless no-ops.

UPDATE mass_notifications.recipients
SET status_after = ''
WHERE status_after IS NULL;

UPDATE mass_notifications.recipients
SET sheet_row = 0
WHERE sheet_row IS NULL;

-- Now safe to add the constraints and defaults.
ALTER TABLE mass_notifications.recipients
    ALTER COLUMN status_after SET DEFAULT '',
    ALTER COLUMN status_after SET NOT NULL;

ALTER TABLE mass_notifications.recipients
    ALTER COLUMN sheet_row SET DEFAULT 0,
    ALTER COLUMN sheet_row SET NOT NULL;


-- 5. Functional indexes that match actual query patterns ---------------------

-- fetchByDate: WHERE c.created_at::date = ?
-- TIMESTAMPTZ::date is not immutable (depends on session timezone),
-- so we pin to UTC for the index expression.
CREATE INDEX IF NOT EXISTS idx_campaigns_created_date
    ON mass_notifications.campaigns ((cast(created_at AT TIME ZONE 'UTC' AS date)));

-- fetchByRecipient: WHERE LOWER(r.email) = LOWER(?)
CREATE INDEX IF NOT EXISTS idx_recipients_email_lower
    ON mass_notifications.recipients (LOWER(email));

-- fetchByActor: WHERE LOWER(c.actor) LIKE LOWER(?)
CREATE INDEX IF NOT EXISTS idx_campaigns_actor_lower
    ON mass_notifications.campaigns (LOWER(actor));

-- fetchByProperty: WHERE LOWER(property_name) LIKE LOWER(?)
CREATE INDEX IF NOT EXISTS idx_campaigns_property_lower
    ON mass_notifications.campaigns (LOWER(property_name));

-- fetchByProperty / fetchByActor also return event_name frequently.
CREATE INDEX IF NOT EXISTS idx_campaigns_event_lower
    ON mass_notifications.campaigns (LOWER(event_name));
