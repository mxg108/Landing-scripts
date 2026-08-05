-- 0003_rbac.sql — per-team/per-role RBAC (pinned design 2026-08-03):
-- roles keyed on the Cf-Access-Jwt-Assertion email Google SSO already
-- injects, admin-editable in-app, NO Engineering involvement. Users never
-- see auth until they hit a restricted page; the denial page offers a
-- self-service access request that lands in qa_access_requests (the /admin
-- page is the inbox until the Slack notification integration is designed).
-- Replaces the interim LOOKUP_ALLOW secret (kept as a grandfather bridge in
-- code until the secret is deleted).

CREATE TABLE qa_roles (
    email       TEXT PRIMARY KEY,     -- SSO email, lowercased
    role        TEXT NOT NULL CHECK (role IN ('admin','qa','manager','viewer')),
    -- NULL = all teams. Reserved for 'manager' scoping (per-team semantics
    -- land when manager capabilities are defined); admin/qa are global.
    team_id     TEXT,
    granted_by  TEXT NOT NULL,
    granted_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    note        TEXT
);

CREATE TABLE qa_access_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL,
    page        TEXT NOT NULL,        -- which wall they hit: 'lookup' | 'score'
    team_id     TEXT,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','denied')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_by TEXT,
    resolved_at TEXT
);

CREATE INDEX idx_access_requests_status ON qa_access_requests (status, created_at);

-- Bootstrap: the QA platform owner. Further grants happen in /admin.
INSERT INTO qa_roles (email, role, granted_by, note)
VALUES ('maximiliano.perez@hellolanding.com', 'admin', 'migration-0003', 'bootstrap admin');
