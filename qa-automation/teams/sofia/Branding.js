/**
 * QA Automation — Branding (Sofia AI)
 *
 * Hand-edited per-team brand and email constants. Loaded BEFORE the
 * auto-generated Config.js (Apps Script loads files alphabetically), so
 * this file declares the `CONFIG` object; Config.js mutates it.
 *
 * Sofia specifics:
 *   - Sofia is Landing's AI voice agent — there is no agent inbox. All
 *     scorecard emails deliver to the reviewing human via
 *     EMAIL.TO_OVERRIDE (Jackson), which src/EmailSender.js honors.
 *   - Config.js regenerates from qa-automation/teams/sofia/
 *     team_config.json (NOT backend/config/teams/ — the Railway backend
 *     glob-loads that directory and must not learn about sofia):
 *       python qa-automation/scripts/build_config.py sofia \
 *         --config qa-automation/teams/sofia/team_config.json
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
  FIRST_EVAL_MESSAGE: 'This is the first QA evaluation for Sofia AI. Future emails will show score trends here.',
  FIRST_EVAL_STYLE:   'font-size:14px;color:{{textGray}};margin:0 0 12px 0;',

  // ── Email defaults ────────────────────────────────────────────────
  EMAIL: {
    SUBJECT_TEMPLATE: 'Sofia AI QA Evaluation — {{date}}',
    MAX_HISTORY:      5,     // how many past QAs to show in ProgressionCard
    // Sofia has no inbox — deliver to the human who owns her review.
    TO_OVERRIDE:      'jackson.chretien@hellolanding.com',
  },

};
