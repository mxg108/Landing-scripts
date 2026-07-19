-- ============================================================================
-- Migration 015: analytics read views (ReadPathFlip §3 W2/W3, slice F5)
--
-- ⚠ ORDERING: apply BEFORE deploying the F5 code — team_source's section
-- query reads qa.v_history_long; deploying first 500s the /team trio.
--
-- W3 — qa.v_history_long: finalized evaluations × sections, the canonical
-- long-form shape. team_source's per-section query is a parameterized
-- slice of it; future feature stores / cohort analyses start here instead
-- of re-deriving the join.
--
-- W2 — qa.v_monthly_scores: per-team calendar-month rollup of finalized
-- overall scores, bucketed in the project TZ. NOTE: this is the
-- UNFILTERED aggregate (no active_only / supervisor awareness — those are
-- per-request dashboard filters that a team-level view cannot honor), so
-- the dashboard chiclets deliberately do NOT read it; it exists for
-- consumers that want team-wide months without a frame load: Command
-- Center, ad-hoc SQL, exports. The 'America/Los_Angeles' literal mirrors
-- team_stats.BUCKET_TZ_NAME's default; if LANDING_BUCKET_TZ ever changes,
-- ship a follow-up migration.
-- ============================================================================

CREATE VIEW qa.v_history_long AS
SELECT e.team_id,
       e.id AS evaluation_id,
       e.agent_id,
       e.agent_name_raw,
       COALESCE(e.call_connected_at, e.approved_at) AS ts,
       e.approved_at,
       e.overall_score,
       e.evaluator_email,
       es.section_id,
       es.section_number,
       es.score_type,
       es.numeric_score,
       es.binary_value
FROM qa.evaluations e
JOIN qa.evaluation_sections es ON es.evaluation_id = e.id
WHERE e.state = 'finalized';

CREATE VIEW qa.v_monthly_scores AS
SELECT e.team_id,
       to_char(COALESCE(e.call_connected_at, e.approved_at)
               AT TIME ZONE 'America/Los_Angeles', 'YYYY-MM') AS year_month,
       COUNT(*)                      AS n,
       AVG(e.overall_score)          AS mean_score,
       STDDEV_SAMP(e.overall_score)  AS std_score
FROM qa.evaluations e
WHERE e.state = 'finalized'
GROUP BY 1, 2;
