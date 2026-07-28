"""Schema-drift guard — eval_store SQL vs the migrations' actual columns.

Born from the 2026-07-27 prod incident: S4 SELECTed ``duration_ms`` from
qa.evaluations, but migration 006 named the column ``call_duration_ms``.
Every unit test passed (fake connections don't validate identifiers) and
every §3 resolution 500'd in prod — rescore, delete, and the restored
scorecard editor all broke at once.

This test derives the real qa.evaluations column set from the migration
files (CREATE TABLE + every ALTER TABLE ... ADD COLUMN) and asserts that
each column referenced by eval_store's qa.evaluations queries exists.
It reads the SQL out of the source file, so a new query with a bad
column fails here before it ever reaches a database.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = _REPO_ROOT / "database" / "migrations"
_EVAL_STORE = (
    Path(__file__).resolve().parents[1] / "backend" / "services" / "eval_store.py"
)

_NON_COLUMN_PREFIXES = (
    "CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "LIKE", "--",
)


def evaluations_columns() -> set[str]:
    """qa.evaluations columns per the migrations (create + alters)."""
    cols: set[str] = set()
    create_sql = (_MIGRATIONS / "006_qa_tables.sql").read_text()
    m = re.search(r"CREATE TABLE qa\.evaluations\s*\((.*?)\n\);", create_sql, re.S)
    assert m, "CREATE TABLE qa.evaluations not found in migration 006"
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.upper().startswith(_NON_COLUMN_PREFIXES):
            continue
        name = line.split()[0].strip('",')
        if name.isidentifier():
            cols.add(name)
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        text = path.read_text()
        for stmt in re.finditer(
                r"ALTER TABLE qa\.evaluations\b[\s\S]*?;", text):
            for add in re.finditer(
                    r"ADD COLUMN (?:IF NOT EXISTS )?(\w+)", stmt.group(0)):
                cols.add(add.group(1))
            for drop in re.finditer(
                    r"DROP COLUMN (?:IF EXISTS )?(\w+)", stmt.group(0)):
                # Down migrations live in *_down.sql — only honor drops
                # from forward migrations.
                if not path.name.endswith("_down.sql"):
                    cols.discard(drop.group(1))
    return cols


def eval_store_evaluations_select_columns() -> dict[str, set[str]]:
    """Column identifiers in each ``SELECT ... FROM qa.evaluations``
    inside eval_store.py, keyed by a snippet of the statement."""
    src = _EVAL_STORE.read_text()
    # Stitch adjacent string literals the way Python does, then find the
    # SELECT ... FROM qa.evaluations statements.
    sql_blobs = re.findall(r'"((?:[^"\\]|\\.)*)"', src)
    stitched = " ".join(sql_blobs)
    out: dict[str, set[str]] = {}
    for m in re.finditer(
            r"SELECT\s+((?:(?!SELECT|INSERT|UPDATE|DELETE\s|FROM\s).)*?)"
            r"\s+FROM qa\.evaluations\b", stitched, re.S):
        col_list = m.group(1)
        if "*" in col_list:
            continue
        cols = set()
        for tok in col_list.split(","):
            tok = tok.strip()
            # Skip function calls / qualified names; plain identifiers only.
            if tok.isidentifier():
                cols.add(tok)
        out[col_list[:60]] = cols
    assert out, "no qa.evaluations SELECTs found — regex drift?"
    return out


def eval_store_evaluations_update_columns() -> set[str]:
    """Columns assigned in ``UPDATE qa.evaluations SET ...`` statements."""
    src = _EVAL_STORE.read_text()
    sql_blobs = re.findall(r'"((?:[^"\\]|\\.)*)"', src)
    stitched = " ".join(sql_blobs)
    cols: set[str] = set()
    for m in re.finditer(
            r"UPDATE qa\.evaluations(?:\s+\w+)?\s+SET\s+(.*?)\s+WHERE",
            stitched, re.S):
        for assign in re.finditer(r"(\w+)\s*=", m.group(1)):
            if assign.group(1).isidentifier():
                cols.add(assign.group(1))
    return cols


def test_migrations_expose_expected_anchor_columns():
    cols = evaluations_columns()
    # Anchors that other code depends on — if parsing breaks, fail here
    # with a clear message rather than passing vacuously.
    for anchor in ("id", "team_id", "overall_score", "call_duration_ms",
                   "auto_rescored_at", "human_review_required_at"):
        assert anchor in cols, f"schema parse lost anchor column {anchor}"
    # The S4 bug: the model-side name must NOT be a real column.
    assert "duration_ms" not in cols


def test_eval_store_selects_only_real_columns():
    schema = evaluations_columns()
    for snippet, cols in eval_store_evaluations_select_columns().items():
        unknown = cols - schema
        assert not unknown, (
            f"eval_store SELECT references non-existent qa.evaluations "
            f"column(s) {sorted(unknown)} in: SELECT {snippet}…"
        )


def test_eval_store_updates_only_real_columns():
    schema = evaluations_columns()
    unknown = eval_store_evaluations_update_columns() - schema
    assert not unknown, (
        f"eval_store UPDATE assigns non-existent qa.evaluations "
        f"column(s): {sorted(unknown)}"
    )
