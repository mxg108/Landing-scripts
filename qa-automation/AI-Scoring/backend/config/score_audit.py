"""Score_Audit tab constants — shared by the writer, init script, and tests.

Per the LookupToScore.md design, there is a single Score_Audit tab on
the member_support spreadsheet (revisit if Sales auditing needs to
diverge). Every successful /score POST, every auth-rejected /score POST,
and every Stage 4 approval append one row here so we can spot-check
which API-key role scored which agent for which team.
"""

from __future__ import annotations


# The audit lives on this team's spreadsheet only.
HOST_TEAM_ID = "member_support"

# Tab name on the host spreadsheet.
TAB_NAME = "Score_Audit"

# Column order — A..J. The init script writes this as the header row;
# append_score_audit_row writes values in the same order.
COLUMNS: list[str] = [
    "timestamp",         # A — ISO 8601 UTC
    "api_key_role",      # B — "team" / "privileged"
    "evaluator_email",   # C — from manager_email form field
    "agent_email",       # D — resolved or supplied
    "agent_name",        # E — display name from Mails or form
    "call_id",           # F — Dialpad call ID
    "target_team",       # G — which team's pipeline ran
    "action",            # H — "scored" / "denied" / "approved"
    "result_row",        # I — FR-AI row number on success; empty on denial
    "notes",             # J — e.g. "no_recording", "rate_limited"
]

# Allowed values for the `action` column. Centralized so call sites
# can't drift; PR 3 will reference these constants directly.
ACTION_SCORED = "scored"
ACTION_DENIED = "denied"
ACTION_APPROVED = "approved"

ACTIONS: frozenset[str] = frozenset({ACTION_SCORED, ACTION_DENIED, ACTION_APPROVED})

# Allowed roles — mirrors KeyIdentity.role in middleware/auth.py.
ROLE_TEAM = "team"
ROLE_PRIVILEGED = "privileged"

ROLES: frozenset[str] = frozenset({ROLE_TEAM, ROLE_PRIVILEGED})
