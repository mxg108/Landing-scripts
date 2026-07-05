-- 013: per-team scoring truth flag (CutoverDesign.md §4)
--
-- 'sheets'   — legacy pipeline is truth: sheet ARRAYFORMULA scores, Postgres
--              dual-writes are shadows (§7.3 Phase A/B, failures swallowed).
-- 'postgres' — the engine scores at approval (compute_overall_score + version
--              stamping), Sheets writes become projections of the DB row, and
--              DB failures are hard errors (§7.3 Phase C).
--
-- The flip and its rollback are one-line UPDATEs on this column — no deploy.
-- Both teams default to 'sheets'; MS flips first after the shadow week,
-- Sales after its 15-section migration bundle (CutoverDesign.md §6).

ALTER TABLE public.teams
    ADD COLUMN scoring_owner TEXT NOT NULL DEFAULT 'sheets'
        CONSTRAINT teams_scoring_owner_check
        CHECK (scoring_owner IN ('sheets', 'postgres'));
