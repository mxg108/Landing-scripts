-- 0008_coaching_tags.sql — CoachingTagsSpec §2 (T1).
-- Typed tag vocabulary over coaching sessions: four FIXED orthogonal
-- supertags (the CHECK enum — adding an axis is deliberately a migration),
-- globally-unique normalized names, soft deprecation with a replaced_by
-- merge pointer, and session↔tag links. Both tables are Sandy-only (no
-- Railway counterpart) — never in shadow_sync WIPE/RESYNC; plain
-- AUTOINCREMENT ids. Cross-team vocabulary by design (owner §8.4: team
-- scoping happens only at RAG-time).

CREATE TABLE qa_coach_tags (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    type               TEXT NOT NULL CHECK (type IN
                           ('sop','system_skills','soft_skills','effectiveness')),
    name               TEXT NOT NULL UNIQUE
                       CHECK (name = lower(trim(name)) AND length(name) > 0),
    description        TEXT,
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','deprecated')),
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deprecated_by      TEXT,
    deprecated_at      TEXT,
    deprecation_note   TEXT,
    replaced_by_tag_id INTEGER REFERENCES qa_coach_tags(id),
    CHECK (status = 'active' OR (deprecated_by IS NOT NULL AND deprecated_at IS NOT NULL))
);
CREATE INDEX idx_coach_tags_active ON qa_coach_tags (type, name) WHERE status = 'active';

CREATE TABLE qa_coaching_tag_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    coaching_id  INTEGER NOT NULL REFERENCES qa_coachings(id) ON DELETE CASCADE,
    tag_id       INTEGER NOT NULL REFERENCES qa_coach_tags(id),
    linked_by    TEXT NOT NULL,
    linked_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_coaching_tag UNIQUE (coaching_id, tag_id)
);
CREATE INDEX idx_coaching_tag_links_tag ON qa_coaching_tag_links (tag_id);

-- Seeds — the owner-refined vocabulary (signed off 2026-08-19).
INSERT INTO qa_coach_tags (type, name, description, created_by) VALUES
    ('sop',            'cancellation_policy',  'Cancellation policy knowledge and application',          'seed'),
    ('sop',            'locked_out',           'Locked-out flow: unit entry, hotel, or temp stay',         'seed'),
    ('sop',            'check_in_procedures',  'Check-in procedures and pre-arrival steps',               'seed'),
    ('system_skills',  'admin',                'Admin tooling and back-office actions',                   'seed'),
    ('system_skills',  'dialpad',              'Dialpad handling: transfers, holds, dispositions',         'seed'),
    ('system_skills',  'mission_control',      'Mission Control navigation and data entry',               'seed'),
    ('soft_skills',    'rudeness',             'Disrespectful or curt tone toward the member',             'seed'),
    ('soft_skills',    'filler_words',         'Excessive fillers and hesitation (uh, buuut…)',           'seed'),
    ('soft_skills',    'tone_matching',        'Matching the member''s emotional register',               'seed'),
    ('effectiveness',  'long_hold',            'Hold time beyond threshold',                              'seed'),
    ('effectiveness',  'no_fcr',               'No first-call resolution',                                'seed'),
    ('effectiveness',  'unresolved_callback',  'Promised callback not delivered / issue left open',        'seed');
