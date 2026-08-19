-- 0009_tag_hierarchy.sql — CoachingTagsSpec §2.1 (T1 amendment).
-- Three kinds, one column: SUPERTAG = the `type` enum; TAG = parent NULL;
-- SUBTAG = parent set (any depth — dialpad → dialpad_transfers →
-- dialpad_transfers_cold are supertag-member TAG, SUBTAG, SUBTAG).
-- Kind is DERIVED, never stored. App-enforced invariants (CHECKs can't
-- read other rows): subtag inherits the parent chain's type; names carry
-- the parent prefix; parents must be active to take children; deprecation
-- cascades down, restore doesn't (and is blocked under a deprecated
-- ancestor). 0008 was already applied live — hierarchy arrives additively.

ALTER TABLE qa_coach_tags ADD COLUMN parent_tag_id INTEGER REFERENCES qa_coach_tags(id);
CREATE INDEX idx_coach_tags_parent ON qa_coach_tags (parent_tag_id)
    WHERE parent_tag_id IS NOT NULL;
