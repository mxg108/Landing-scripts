/**
 * QA Automation — Configuration
 * Landing QA System v1.0.0
 *
 * Replace placeholder IDs before first clasp push.
 */

// ── Google Resource IDs ─────────────────────────────────────────────
const CONFIG = {
  // ── Sheet names ─────────────────────────────────────────────────
  QA_SHEET_NAME:       'Form Responses 1',   // default name Google gives the linked sheet
  HISTORY_SHEET_NAME:  'Analyst_History',

  // ── QA Sheet column indices (0-based) ───────────────────────────
  COL: {
    TIMESTAMP:           0,   // A
    MANAGER_EMAIL:       1,   // B
    AGENT_NAME:          2,   // C
    GREETING:            3,   // D  (1-5)
    IDENTITY_VALIDATION: 4,   // E  (Y/N)
    CALL_PURPOSE:        5,   // F  (1-5)
    MATCH_MOMENT:        6,   // G  (1-5)
    PROCESS_ADHERENCE:   7,   // H  (1-5)
    CALL_RESOLUTION:     8,   // I  (1-5)
    COMMUNICATION:       9,   // J  (1-5)
    EFFICIENCY:          10,  // K  (1-5)
    DOCUMENTATION:       11,  // L  (1-5)
    CUSTOMER_RESOLUTION: 12,  // M  (Y/N)
    STRENGTHS:           13,  // N  (text)
    IMPROVEMENTS:        14,  // O  (text)
    DIALPAD_LINK:        15,  // P  (URL)
    OVERALL_SCORE:       16,  // Q  (auto-calculated)
    AGENT_EMAIL:         21,  // V
  },

  // ── Numeric score categories (1-5 scale) ────────────────────────
  NUMERIC_CATEGORIES: [
    { key: 'greeting',         label: 'Greeting',                    col: 3  },
    { key: 'callPurpose',      label: 'Purpose of the Call',         col: 5  },
    { key: 'matchMoment',      label: 'Matching the Moment',         col: 6  },
    { key: 'processAdherence', label: 'Process Adherence',           col: 7  },
    { key: 'callResolution',   label: 'Call Resolution',             col: 8  },
    { key: 'communication',    label: 'Communication',               col: 9  },
    { key: 'efficiency',       label: 'Efficiency & Call Handling',   col: 10 },
    { key: 'documentation',    label: 'Documentation',               col: 11 },
  ],

  // ── Binary categories (Y/N) ─────────────────────────────────────
  BINARY_CATEGORIES: [
    { key: 'identityValidation',  label: 'Caller Identity Validation',   col: 4  },
    { key: 'customerResolution',  label: 'Customer Resolution Indicator', col: 12 },
  ],

  // ── Score thresholds for color coding ───────────────────────────
  THRESHOLDS: {
    HIGH:   4,   // 4-5 → accent blue
    MID:    3,   // 3   → amber
                 // 1-2 → red
  },

  // ── Landing brand colors ────────────────────────────────────────
  COLORS: {
    DARK_NAVY:   '#15192D',
    ACCENT_BLUE: '#1A61D9',
    LIGHT_BLUE:  '#E7EFFB',
    WHITE:       '#FFFFFF',
    AMBER:       '#E8A317',
    RED:         '#D9534F',
    GREEN:       '#28A745',
    TEXT_GRAY:   '#4A4A4A',
  },

  // ── Email defaults ──────────────────────────────────────────────
  EMAIL: {
    SUBJECT_TEMPLATE: 'QA Evaluation — {{agentName}} — {{date}}',
    MAX_HISTORY:      5,     // how many past QAs to show in ProgressionCard
  },

  // ── Analyst_History sheet columns (0-based) ─────────────────────
  HISTORY_COL: {
    AGENT_NAME:     0,   // A
    AGENT_EMAIL:    1,   // B
    TIMESTAMP:      2,   // C
    OVERALL_SCORE:  3,   // D
    GREETING:       4,   // E
    CALL_PURPOSE:   5,   // F
    MATCH_MOMENT:   6,   // G
    PROCESS:        7,   // H
    RESOLUTION:     8,   // I
    COMMUNICATION:  9,   // J
    EFFICIENCY:     10,  // K
    DOCUMENTATION:  11,  // L
    IDENTITY_VAL:   12,  // M  (Y/N)
    CUSTOMER_RES:   13,  // N  (Y/N)
    MANAGER_EMAIL:  14,  // O
  },
};
