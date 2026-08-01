"""Pure-stage tests for scripts/pulpo_compendium_export.py — diff/résumé
logic and the HTML build invariants that make the Google Doc's index
actually clickable (the <a name> bookmark contract)."""

import importlib.util
import re
import sys
from pathlib import Path

_AI_SCORING = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "pulpo_compendium_export",
    _AI_SCORING / "scripts" / "pulpo_compendium_export.py",
)
comp = importlib.util.module_from_spec(_spec)
sys.modules["pulpo_compendium_export"] = comp
_spec.loader.exec_module(comp)


def _corpus(docs):
    return {"stats": {"fetched": len(docs)},
            "docs": [{"id": d["id"], "listing": {}, "sources": [],
                      "document": d} for d in docs]}


def _doc(i, **over):
    d = {
        "id": f"id-{i}",
        "title": f"Doc {i}",
        "body": f"Body of doc {i}",
        "tags": ["nmt"],
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z",
        "last_verified_at": "2026-07-02T00:00:00Z",
        "next_review_at": "2026-08-02T00:00:00Z",
        "review_cadence": "monthly",
        "status": "verified",
        "owner_name": "Owner",
        "audiences": ["internal"],
        "open_flags": [],
        "url": f"https://example.test/docs/id-{i}",
        "body_format": "markdown",
    }
    d.update(over)
    return d


# -- diff / résumé -----------------------------------------------------

def test_diff_detects_added_removed_updated_and_flags():
    prev = _corpus([_doc(1), _doc(2), _doc(3)])
    curr = _corpus([
        _doc(1),                                              # unchanged
        _doc(2, updated_at="2026-07-30T00:00:00Z",            # updated
             open_flags=[{"quote": "q", "body": "b"}]),       # + flag
        _doc(4),                                              # added
    ])                                                        # 3 removed
    d = comp.diff_corpora(prev, curr)
    assert d["added"] == ["Doc 4"]
    assert d["removed"] == ["Doc 3"]
    assert d["updated"] == ["Doc 2"]
    assert d["flag_changes"] == ["Doc 2 (0→1 open flags)"]
    assert d["total_docs"] == 3


def test_diff_resume_no_changes():
    c = _corpus([_doc(1)])
    assert comp.diff_resume(comp.diff_corpora(c, c)) == "1 docs — no changes"


def test_diff_resume_clips_long_lists():
    prev = _corpus([])
    curr = _corpus([_doc(i) for i in range(1, 7)])
    resume = comp.diff_resume(comp.diff_corpora(prev, curr))
    assert "6 new" in resume
    assert "+3 more" in resume


# -- build invariants --------------------------------------------------

def test_build_html_anchor_contract():
    """Every doc gets exactly one <a name='doc-N'> bookmark, and every
    internal href targets an emitted bookmark (index + tag map)."""
    corpus = _corpus([_doc(1), _doc(2, tags=[]),
                      _doc(3, body_format="flowchart", body="flowchart TD")])
    html = comp.build_html(corpus, generated_on="2026-07-31")
    names = re.findall(r"<a name='(doc-\d+)'></a>", html)
    assert names == ["doc-1", "doc-2", "doc-3"]
    hrefs = set(re.findall(r"href='#(doc-\d+)'", html))
    assert hrefs == set(names)


def test_build_html_untagged_and_flowchart_sections():
    corpus = _corpus([_doc(1, tags=[]),
                      _doc(2, body_format="flowchart",
                           body="flowchart TD\n  A --> B")])
    html = comp.build_html(corpus, generated_on="2026-07-31")
    assert "UNTAGGED" in html
    assert "<pre>flowchart TD" in html          # Mermaid stays source, escaped
    assert "flowchart (Mermaid)" in html


def test_build_html_escapes_titles_and_renders_flags():
    corpus = _corpus([_doc(1, title="A <b>& risky</b> title",
                           open_flags=[{"quote": "the quote",
                                        "body": "why",
                                        "suggestion": "fix it"}])])
    html = comp.build_html(corpus, generated_on="2026-07-31")
    assert "A &lt;b&gt;&amp; risky&lt;/b&gt; title" in html
    assert "the quote" in html and "fix it" in html
    assert "open review flag" in html
