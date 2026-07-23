"""Notion decommission grep gate — PulpoConnection §8 (P4).

The keyword→Notion-page SOP path was deleted 2026-07-23; the RAG
provider (Pulpo behind the §4.1 seam) is the only SOP source. This gate
keeps it deleted: no `notion` reference may reappear under backend/ —
the same guard pattern as the qa_scoring regression gate.
"""

from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"


def test_no_notion_references_in_backend():
    offenders = []
    for path in _BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "notion" in text:
            offenders.append(str(path.relative_to(_BACKEND)))
    assert not offenders, (
        f"Notion was decommissioned (PulpoConnection §8) but these "
        f"backend files reference it: {offenders}"
    )
