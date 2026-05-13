"""Generate Apps Script Config.js from a team's Python TeamConfig.

Usage:
    python qa-automation/scripts/build_config.py <team_id>

Reads:
    qa-automation/AI-Scoring/backend/config/teams/<team_id>.json

Writes:
    qa-automation/teams/<team_id>/Config.js

The generated file mutates a `CONFIG` object that the team's `Branding.js`
declares first (Apps Script loads files alphabetically — Branding < Config).
Brand colors, email templates, QA goal, and thresholds stay in `Branding.js`
and are NOT touched by this generator.

Contents generated:
- Sheet names (HISTORY_SHEET_NAME, MAILS_SHEET_NAME)
- HISTORY_LAYOUT — column-position constants derived from N = len(sections)
- NUMERIC_CATEGORIES — numeric+manual sections; key=section.id, label=name,
  historyIdx=position in the derived layout's score range
- BINARY_CATEGORIES — yn sections (incl. auto_value)
- MANUAL_CATEGORIES — manual-only subset of NUMERIC_CATEGORIES
- SECTION_LABELS — {history_id: name}
- RUBRIC_QUESTIONS — {history_id: rubric_question}

Run after editing the team JSON config; commit the regenerated Config.js
alongside the JSON change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo paths
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_AI_SCORING = _REPO_ROOT / "qa-automation" / "AI-Scoring"
_TEAMS_DIR = _REPO_ROOT / "qa-automation" / "teams"

# Make backend.* importable
sys.path.insert(0, str(_AI_SCORING))

from backend.config.team_config import TeamConfig, get_team_config  # noqa: E402


def _js_string(value: str | None) -> str:
    """JSON-encode a string for safe embedding in JS source."""
    return json.dumps(value if value is not None else "")


def _render_history_layout(config: TeamConfig) -> str:
    L = config.history_layout
    lines = [
        "CONFIG.HISTORY_LAYOUT = {",
        f"  N_SECTIONS:         {L.n},",
        f"  TOTAL_WIDTH:        {L.total_width},",
        f"  COL_AGENT_NAME:     0,",
        f"  COL_AGENT_EMAIL:    1,",
        f"  COL_TIMESTAMP:      2,",
        f"  COL_EVALUATOR_EMAIL:3,",
        f"  COL_DIALPAD_LINK:   4,",
        f"  COL_OVERALL_SCORE:  5,",
        f"  SCORES_START:       {L.scores_start},",
        f"  SCORES_END:         {L.scores_end},",
        f"  REASONING_START:    {L.reasoning_start},",
        f"  REASONING_END:      {L.reasoning_end},",
        f"  CONFIDENCE_START:   {L.confidence_start},",
        f"  CONFIDENCE_END:     {L.confidence_end},",
        f"  COL_KEY_STRENGTHS:  {L.col_key_strengths},",
        f"  COL_OPPORTUNITIES:  {L.col_opportunities},",
        f"  COL_CALL_SUMMARY:   {L.col_call_summary},",
        f"  COL_CALLER_NAME:    {L.col_caller_name},",
        f"  COL_CALLER_PHONE:   {L.col_caller_phone},",
        f"  COL_SOURCE:         {L.col_source},",
        "};",
    ]
    return "\n".join(lines)


def _render_categories(config: TeamConfig) -> str:
    """NUMERIC_CATEGORIES, BINARY_CATEGORIES, MANUAL_CATEGORIES."""
    sections_in_order = config.sections_by_number
    history_idx_by_id = {s.id: i for i, s in enumerate(sections_in_order)}

    def cat_obj(s) -> str:
        return (
            f"  {{ key: {_js_string(s.id)}, "
            f"label: {_js_string(s.name)}, "
            f"historyIdx: {history_idx_by_id[s.id]} }}"
        )

    def cat_block(label: str, sections: list) -> str:
        if not sections:
            return f"CONFIG.{label} = [];"
        body = ",\n".join(cat_obj(s) for s in sections)
        return f"CONFIG.{label} = [\n{body},\n];"

    # NUMERIC_CATEGORIES = numeric + manual (both 1-5 scores).
    # Order by section_number so the email score card renders sections in
    # rubric order. Manual sections appear inline alongside AI-scored ones.
    numeric_or_manual = [
        s for s in sections_in_order if s.score_type in ("numeric", "manual")
    ]
    yn_sections = [s for s in sections_in_order if s.score_type == "yn"]
    manual_only = [s for s in sections_in_order if s.score_type == "manual"]

    return "\n\n".join([
        cat_block("NUMERIC_CATEGORIES", numeric_or_manual),
        cat_block("BINARY_CATEGORIES", yn_sections),
        cat_block("MANUAL_CATEGORIES", manual_only),
    ])


def _render_label_map(label: str, pairs: list[tuple[str, str | None]]) -> str:
    if not pairs:
        return f"CONFIG.{label} = {{}};"
    body = ",\n".join(f"  {_js_string(k)}: {_js_string(v)}" for k, v in pairs)
    return f"CONFIG.{label} = {{\n{body},\n}};"


def _render_section_labels(config: TeamConfig) -> str:
    return _render_label_map(
        "SECTION_LABELS",
        [(s.history_id, s.name) for s in config.sections_by_number],
    )


def _render_rubric_questions(config: TeamConfig) -> str:
    return _render_label_map(
        "RUBRIC_QUESTIONS",
        [(s.history_id, s.rubric_question) for s in config.sections_by_number],
    )


def _render_sheet_names(config: TeamConfig) -> str:
    sheets = config.sheets
    return "\n".join([
        f"CONFIG.HISTORY_SHEET_NAME = {_js_string(sheets.analyst_history.tab_name)};",
        f"CONFIG.MAILS_SHEET_NAME   = {_js_string(sheets.mails_tab)};",
    ])


def render_config_js(team_id: str, config: TeamConfig) -> str:
    """Render the full Config.js text for a team."""
    parts = [
        "/**",
        " * QA Automation — Config (auto-generated)",
        " *",
        f" * Team: {config.display_name} ({team_id})",
        f" * Rubric version: {config.rubric_version}",
        " *",
        " * AUTO-GENERATED — DO NOT EDIT.",
        " * Run: python qa-automation/scripts/build_config.py " + team_id,
        " *",
        " * Mutates the CONFIG object declared in Branding.js (loads first",
        " * alphabetically). Brand colors, email templates, QA goal, and",
        " * thresholds live in Branding.js and are not touched here.",
        " */",
        "",
        "// ── Sheet names ──────────────────────────────────────────────",
        _render_sheet_names(config),
        "",
        "// ── Derived row layout (from HistoryLayout(N)) ───────────────",
        _render_history_layout(config),
        "",
        "// ── Section partitions ───────────────────────────────────────",
        "// `key` is the section id (also matches history_id when they're equal),",
        "// `historyIdx` is the position in the derived layout's score range.",
        _render_categories(config),
        "",
        "// ── Section labels + rubric prompts ──────────────────────────",
        _render_section_labels(config),
        "",
        _render_rubric_questions(config),
    ]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("team_id", help="Team id (matches teams/{id}.json)")
    args = parser.parse_args()

    team_id = args.team_id
    out_dir = _TEAMS_DIR / team_id
    out_path = out_dir / "Config.js"

    if not out_dir.exists():
        print(f"Error: team directory not found: {out_dir}", file=sys.stderr)
        return 1

    config = get_team_config(team_id)
    text = render_config_js(team_id, config)
    out_path.write_text(text, encoding="utf-8")

    print(f"Wrote {out_path} ({len(text)} bytes, N={len(config.sections)} sections)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
