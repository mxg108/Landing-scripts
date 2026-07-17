"""Sheet-vs-Postgres frame comparison (ReadPathFlip §5).

Key derivation + comparator for the golden-parity harness
(scripts/parity_readpath.py), which layers the nine compute_* functions
and the endpoint permutation grid on top. During the F4 shadow window
this module also drove the live per-request compare; the flip completed
2026-07-15 and F5 deleted the shadow path, so the harness — still useful
while the Analyst_History sheet is written as a projection — is the sole
consumer.

Membership deltas are EXPECTED and not failures: sheet-only rows (B0
hard-zero exclusions) and db-only rows (backfilled history the sheet
dropped). Only cell diffs on the common set signal a real divergence.
"""

from __future__ import annotations

import math
import unicodedata

import pandas as pd


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(s))
                   if not unicodedata.combining(c))


# Columns that legitimately differ between the two sources (see
# classify_cell_diff): clocks were B2-repaired in the DB, roster fields go
# stale in qa.agents, and agent spelling is accent-folded there. Everything
# ELSE is expected byte-identical — those are the "stable content" columns.
_CLOCK_COLS = {"timestamp", "eval_approved_at"}
_ROSTER_COLS = {"is_active", "supervisor"}
_JITTER_COLS = _CLOCK_COLS | _ROSTER_COLS | {"agent"}


def key_series(df: pd.DataFrame) -> list[str]:
    """Row key eval_id|agent|ordinal — agent accent-stripped (roster
    spellings drift), ordinal disambiguates D2 re-eval pairs sharing an
    eval_id.

    The ordinal orders same-key rows by their STABLE CONTENT (every shared
    column except the known-jittery ones — clocks were B2-repaired in the
    DB, roster/agent-spelling drift in qa.agents). Sorting by anything
    jittery cross-pairs a D2 pair whose rows are otherwise tied: observed
    twice on MS — first a pair tied on eval_id whose ±1s clock repair
    flipped a timestamp tiebreak, then a pair ALSO tied on overall_score
    and second-truncated timestamp, where sub-second jitter decided.

    Pairing by content is sound, not self-fulfilling: within a same
    (eval_id, agent) group every compute_* aggregates per-agent, so
    intra-group order is analytically irrelevant — canonical ordering on
    both sides pairs identical rows, and any REAL value difference still
    has nowhere to hide (the multiset of rows differs)."""
    base = (df["eval_id"].astype(str) + "|"
            + df["agent"].str.strip().str.lower().map(strip_accents))
    stable = [c for c in sorted(df.columns) if c not in _JITTER_COLS]
    order = df.assign(_b=base).sort_values(["_b", *stable])
    ordinal = order.groupby("_b").cumcount()
    return (base + "|" + ordinal.reindex(df.index).astype(str)).tolist()


def norm_cell(v):
    """Canonicalize a cell for equality: NaN→None, datetimes→sec-ISO."""
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat(timespec="seconds")
    return v


def align(sheet_df: pd.DataFrame, pg_df: pd.DataFrame):
    """Align on the row key; return (membership, sub_sheet, sub_pg).

    The two sub-frames hold only the common rows, in identical (key-sorted)
    order with a clean RangeIndex."""
    s = sheet_df.assign(_k=key_series(sheet_df)).set_index("_k")
    p = pg_df.assign(_k=key_series(pg_df)).set_index("_k")
    sheet_only = sorted(set(s.index) - set(p.index))
    db_only = sorted(set(p.index) - set(s.index))
    common = sorted(set(s.index) & set(p.index))
    membership = {
        "sheet": len(s), "pg": len(p), "common": len(common),
        "sheet_only": len(sheet_only), "db_only": len(db_only),
        "sheet_only_keys": [k.split("|") for k in sheet_only[:25]],
        "db_only_sample": [k.split("|") for k in db_only[:5]],
    }
    return membership, s.loc[common].reset_index(drop=True), p.loc[common].reset_index(drop=True)


def cell_diffs(sub_sheet: pd.DataFrame, sub_pg: pd.DataFrame) -> list[dict]:
    """Full column-by-column diff of the aligned common rows (shared
    columns only). Returns every differing cell — callers slice/classify."""
    cols = [c for c in sub_sheet.columns if c in sub_pg.columns]
    diffs = []
    for i in range(len(sub_sheet)):
        a, b = sub_sheet.iloc[i], sub_pg.iloc[i]
        for c in cols:
            va, vb = norm_cell(a[c]), norm_cell(b[c])
            if va != vb:
                diffs.append({"row": i, "col": c, "sheet": va, "pg": vb})
    return diffs


# Cell-diff classes for the read-path flip: clock / name_accent / roster
# (the _JITTER_COLS above) are KNOWN source-of-truth differences between
# the sheet and qa.*, NOT row-source bugs — so the sweep verdict (and the
# live-shadow alarm) key on 'other'.

def classify_cell_diff(d: dict) -> str:
    """Bucket one cell diff:

    - ``clock``       timestamp column; B2 repaired the DB clock from
      Dialpad while the sheet kept the stale value — DB is authoritative.
    - ``name_accent`` the agent name is equal once accents are folded;
      qa.agents stores the canonical (accent-stripped) spelling.
    - ``roster``      is_active / supervisor; qa.agents roster is stale vs
      the Mails tab — the one ACTIONABLE class (refresh qa.agents before
      flipping so the dashboard's active/supervisor columns stay correct).
    - ``other``       anything else: a genuine row-source divergence that
      must be zero for parity.
    """
    col = d["col"]
    if col in _CLOCK_COLS:
        return "clock"
    if col == "agent":
        if (strip_accents(str(d["sheet"])).strip().lower()
                == strip_accents(str(d["pg"])).strip().lower()):
            return "name_accent"
        return "other"
    if col in _ROSTER_COLS:
        return "roster"
    return "other"


def classify_diffs(diffs: list[dict]) -> dict:
    """Group diffs by class → {class: [diffs]}."""
    buckets: dict[str, list] = {"clock": [], "name_accent": [], "roster": [], "other": []}
    for d in diffs:
        buckets[classify_cell_diff(d)].append(d)
    return buckets


def compare(sheet_df: pd.DataFrame, pg_df: pd.DataFrame) -> dict:
    """Membership + classified common-row cell-diff summary (no compute_*)."""
    membership, sub_sheet, sub_pg = align(sheet_df, pg_df)
    diffs = cell_diffs(sub_sheet, sub_pg)
    buckets = classify_diffs(diffs)
    return {"membership": membership, "cell_diff_count": len(diffs),
            "classified": {k: len(v) for k, v in buckets.items()},
            "cell_diff_sample": diffs[:50]}

