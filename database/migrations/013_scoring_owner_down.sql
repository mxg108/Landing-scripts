-- 013 down: remove the scoring truth flag.
--
-- Only safe while every team is still (or back) on 'sheets' — dropping the
-- column while a team is flipped would silently revert it to the legacy
-- pipeline on the next deploy.

ALTER TABLE public.teams
    DROP COLUMN scoring_owner;
