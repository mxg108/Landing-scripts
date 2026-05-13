/**
 * QA Automation — Branding (Member Support)
 *
 * Hand-edited per-team brand and email constants. Loaded BEFORE the
 * auto-generated Config.js (Apps Script loads files alphabetically), so
 * this file declares the `CONFIG` object; Config.js mutates it.
 *
 * What lives here (NOT touched by build_config.py):
 *   - COLORS: Landing brand palette
 *   - EMAIL: subject template + history-card depth
 *   - QA_GOAL: pass-mark on the 0–100 overall scale
 *   - THRESHOLDS: color-code cutoffs for category and overall scores
 *   - FIRST_EVAL_MESSAGE / FIRST_EVAL_STYLE: progression-card empty state
 *
 * Edit freely. Schema-driven values (sheet names, layout positions,
 * section partitions, labels, rubric questions) are in Config.js and
 * regenerated from backend/config/teams/member_support.json.
 */

var CONFIG = {

  // ── QA goal (0-100 scale) ─────────────────────────────────────────
  QA_GOAL: 85,

  // ── Score thresholds for color coding ─────────────────────────────
  THRESHOLDS: {
    OVERALL_HIGH: 85,     // >= 85 → green   (0-100 scale)
    OVERALL_MID:  70,     // >= 70 → amber
    CATEGORY_HIGH: 4.25,  // >= 4.25 → green (1-5 scale, ≈85%)
    CATEGORY_MID:  3.5,   // >= 3.5  → amber (1-5 scale, ≈70%)
  },

  // ── Landing brand colors ──────────────────────────────────────────
  COLORS: {
    DARK_NAVY:   '#15192D',
    ACCENT_BLUE: '#1A61D9',
    LIGHT_BLUE:  '#E7EFFB',
    WHITE:       '#FFFFFF',
    AMBER:       '#E8A317',
    RED:         '#D9534F',
    GREEN:       '#28A745',
    TEXT_GRAY:   '#4A4A4A',
    GOLD:        '#FFD700',
    GOLD_DARK:   '#B8860B',
    GOLD_LIGHT:  '#FFF8E1',
    GREEN_LIGHT: '#E8F5E9',
  },

  // ── First evaluation messaging ────────────────────────────────────
  FIRST_EVAL_MESSAGE: 'This is {{agentName}}\'s first QA evaluation. Future emails will show score trends here.',
  FIRST_EVAL_STYLE:   'font-size:14px;color:{{textGray}};margin:0 0 12px 0;',

  // ── Email defaults ────────────────────────────────────────────────
  EMAIL: {
    SUBJECT_TEMPLATE: 'QA Evaluation — {{agentName}} — {{date}}',
    MAX_HISTORY:      5,     // how many past QAs to show in ProgressionCard
  },

};
