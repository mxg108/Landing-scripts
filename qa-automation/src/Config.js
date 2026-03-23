/**
 * QA Automation — Configuration
 * Landing QA System v1.1
 *
 * Replace placeholder IDs before first clasp push.
 */

// ── Google Resource IDs ─────────────────────────────────────────────
const CONFIG = {
  // ── Sheet names ─────────────────────────────────────────────────
  QA_SHEET_NAME:       'Form Responses 1',   // default name Google gives the linked sheet
  HISTORY_SHEET_NAME:  'Analyst_History',
  MAILS_SHEET_NAME:    'Mails',              // source of truth for agent name → email mapping
  FORM_AI_SHEET_NAME:  'Form Responses AI',

  // ── QA goal (0-100 scale) ─────────────────────────────────────
  QA_GOAL: 85,

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
    AGENT_EMAIL:         21,  // V  (Mails — ARRAYFORMULA lookup)
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
    OVERALL_HIGH: 85,     // >= 85 → green   (0-100 scale)
    OVERALL_MID:  70,     // >= 70 → amber
    CATEGORY_HIGH: 4.25,  // >= 4.25 → green (1-5 scale, ≈85%)
    CATEGORY_MID:  3.5,   // >= 3.5  → amber (1-5 scale, ≈70%)
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
    GOLD:        '#FFD700',
    GOLD_DARK:   '#B8860B',
    GOLD_LIGHT:  '#FFF8E1',
    GREEN_LIGHT: '#E8F5E9',
  },

  // ── First evaluation messaging ──────────────────────────────────
  FIRST_EVAL_MESSAGE: 'This is {{agentName}}\'s first QA evaluation. Future emails will show score trends here.',
  FIRST_EVAL_STYLE:   'font-size:14px;color:{{textGray}};margin:0 0 12px 0;',

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
    DIALPAD_LINK:       15,  // P
    KEY_STRENGTHS:      16,  // Q
    IMPROVEMENTS:       17,  // R
    SOURCE:             18,  // S
    GREETING_CONF:      19,  // T
    GREETING_REASON:    20,  // U
    IDENTITY_CONF:      21,  // V
    IDENTITY_REASON:    22,  // W
    PURPOSE_CONF:       23,  // X
    PURPOSE_REASON:     24,  // Y
    MATCHING_CONF:      25,  // Z
    MATCHING_REASON:    26,  // AA
    PROCESS_CONF:       27,  // AB
    PROCESS_REASON:     28,  // AC
    RESOLUTION_CONF:    29,  // AD
    RESOLUTION_REASON:  30,  // AE
    COMMUNICATION_CONF: 31,  // AF
    COMMUNICATION_REASON: 32, // AG
    EFFICIENCY_CONF:    33,  // AH
    EFFICIENCY_REASON:  34,  // AI
    CUSTOMER_RES_CONF:  35,  // AJ
    CUSTOMER_RES_REASON: 36, // AK
  },

  // ── Form Responses AI extended columns (0-based) ──────────────────
  FORM_AI_COL: {
    DIALPAD_LINK:       15,  // P — match key for lookup
    SOURCE:             16,  // Q
    GREETING_CONF:      17,  // R
    GREETING_REASON:    18,  // S
    IDENTITY_CONF:      19,  // T
    IDENTITY_REASON:    20,  // U
    PURPOSE_CONF:       21,  // V
    PURPOSE_REASON:     22,  // W
    MATCHING_CONF:      23,  // X
    MATCHING_REASON:    24,  // Y
    PROCESS_CONF:       25,  // Z
    PROCESS_REASON:     26,  // AA
    RESOLUTION_CONF:    27,  // AB
    RESOLUTION_REASON:  28,  // AC
    COMMUNICATION_CONF: 29,  // AD
    COMMUNICATION_REASON: 30, // AE
    EFFICIENCY_CONF:    31,  // AF
    EFFICIENCY_REASON:  32,  // AG
    CUSTOMER_RES_CONF:  33,  // AH
    CUSTOMER_RES_REASON: 34, // AI
  },
};
