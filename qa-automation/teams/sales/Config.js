/**
 * QA Automation — Config (auto-generated)
 *
 * Team: Sales (sales)
 * Rubric version: 1.0
 *
 * AUTO-GENERATED — DO NOT EDIT.
 * Run: python qa-automation/scripts/build_config.py sales
 *
 * Mutates the CONFIG object declared in Branding.js (loads first
 * alphabetically). Brand colors, email templates, QA goal, and
 * thresholds live in Branding.js and are not touched here.
 */

// ── Sheet names ──────────────────────────────────────────────
CONFIG.QA_SHEET_NAME      = "Scores";
CONFIG.HISTORY_SHEET_NAME = "Analyst_History";
CONFIG.MAILS_SHEET_NAME   = "Mails";
CONFIG.FORM_AI_SHEET_NAME = "Form Responses AI";

// ── Derived row layout (from HistoryLayout(N)) ───────────────
CONFIG.HISTORY_LAYOUT = {
  N_SECTIONS:         19,
  TOTAL_WIDTH:        69,
  COL_AGENT_NAME:     0,
  COL_AGENT_EMAIL:    1,
  COL_TIMESTAMP:      2,
  COL_EVALUATOR_EMAIL:3,
  COL_DIALPAD_LINK:   4,
  COL_OVERALL_SCORE:  5,
  SCORES_START:       6,
  SCORES_END:         25,
  REASONING_START:    25,
  REASONING_END:      44,
  CONFIDENCE_START:   44,
  CONFIDENCE_END:     63,
  COL_KEY_STRENGTHS:  63,
  COL_OPPORTUNITIES:  64,
  COL_CALL_SUMMARY:   65,
  COL_CALLER_NAME:    66,
  COL_CALLER_PHONE:   67,
  COL_SOURCE:         68,
};

// ── Section partitions ───────────────────────────────────────
// `key` is the section id (also matches history_id when they're equal),
// `historyIdx` is the position in the derived layout's score range,
// `col` is the 0-based column index on the score-destination tab
// (FR1 for MS, Scores for Sales) — used by the legacy bridge path
// in QAEntry's FR1 constructor; drop with the bridge cleanup.
CONFIG.NUMERIC_CATEGORIES = [
  { key: "pb_creation", label: "PB Created", col: 4, historyIdx: 1 },
  { key: "mc_call_notes", label: "MC Call Notes", col: 5, historyIdx: 2 },
  { key: "situation_match", label: "Situation Match (First 2 Min)", col: 6, historyIdx: 3 },
  { key: "value_uplift", label: "Landing Value Uplift", col: 8, historyIdx: 5 },
  { key: "landing_guarantee", label: "Landing Guarantee Explanation", col: 11, historyIdx: 8 },
  { key: "objection_handling", label: "Objection Handling", col: 14, historyIdx: 11 },
];

CONFIG.BINARY_CATEGORIES = [
  { key: "greeting", label: "Greeting & Lead Name", col: 3, historyIdx: 0 },
  { key: "reason_for_move_pitch", label: "Reason as Sales Argument", col: 7, historyIdx: 4 },
  { key: "membership_explanation", label: "Membership Explanation", col: 9, historyIdx: 6 },
  { key: "flex_long_stay_pitch", label: "FLEX Pitch (60+ Nights)", col: 10, historyIdx: 7 },
  { key: "pricing_explanation", label: "Pricing Breakdown", col: 12, historyIdx: 9 },
  { key: "book_attempt", label: "Asked to Book on Call", col: 13, historyIdx: 10 },
  { key: "urgency_disclosure", label: "Pricing/Inventory Urgency", col: 15, historyIdx: 12 },
  { key: "followup_setup", label: "Follow-up Set", col: 16, historyIdx: 13 },
  { key: "tonality_pace", label: "Tonality & Pace", col: 17, historyIdx: 14 },
  { key: "hold_usage", label: "Hold Usage / Dead Air", col: 18, historyIdx: 15 },
  { key: "audio_quality", label: "Audio Quality", col: 19, historyIdx: 16 },
  { key: "screen_recording", label: "Screen Recording (Inbound Only)", col: 20, historyIdx: 17 },
  { key: "pre_send_intro", label: "Pre-Send Intro", col: 21, historyIdx: 18 },
];

CONFIG.MANUAL_CATEGORIES = [
  { key: "pb_creation", label: "PB Created", col: 4, historyIdx: 1 },
  { key: "mc_call_notes", label: "MC Call Notes", col: 5, historyIdx: 2 },
];

// ── Section labels + rubric prompts ──────────────────────────
CONFIG.SECTION_LABELS = {
  "greeting": "Greeting & Lead Name",
  "pb_creation": "PB Created",
  "mc_call_notes": "MC Call Notes",
  "situation_match": "Situation Match (First 2 Min)",
  "reason_for_move_pitch": "Reason as Sales Argument",
  "value_uplift": "Landing Value Uplift",
  "membership_explanation": "Membership Explanation",
  "flex_long_stay_pitch": "FLEX Pitch (60+ Nights)",
  "landing_guarantee": "Landing Guarantee Explanation",
  "pricing_explanation": "Pricing Breakdown",
  "book_attempt": "Asked to Book on Call",
  "objection_handling": "Objection Handling",
  "urgency_disclosure": "Pricing/Inventory Urgency",
  "followup_setup": "Follow-up Set",
  "tonality_pace": "Tonality & Pace",
  "hold_usage": "Hold Usage / Dead Air",
  "audio_quality": "Audio Quality",
  "screen_recording": "Screen Recording (Inbound Only)",
  "pre_send_intro": "Pre-Send Intro",
};

CONFIG.RUBRIC_QUESTIONS = {
  "greeting": "Did the agent say their name and 'Landing', and use the lead's name in the greeting?",
  "pb_creation": "",
  "mc_call_notes": "",
  "situation_match": "Did the agent get, react to, and match the situation behind the reason for the move within the first 2 minutes?",
  "reason_for_move_pitch": "Did the agent use the reason for the move as a sales argument throughout the call?",
  "value_uplift": "Did the agent uplift Landing's value against other booking options and prices?",
  "membership_explanation": "Did the agent explain the Landing Membership correctly and offer the best booking option?",
  "flex_long_stay_pitch": "If the prospect's intended stay is greater than 60 nights, did the agent pitch FLEX first?",
  "landing_guarantee": "After the tour objection, did the agent refer to the Landing Guarantee and explain it correctly?",
  "pricing_explanation": "Did the agent understand the situation and offer the best pricing breakdown explanation?",
  "book_attempt": "Did the agent ask if the prospect wanted to book on this call?",
  "objection_handling": "Was the agent able to address all concerns and handle objections?",
  "urgency_disclosure": "Did the agent mention that prices can change and the apartment can get taken online?",
  "followup_setup": "Did the agent set up a specific medium and time for a follow-up?",
  "tonality_pace": "Did the agent use a friendly, customer-oriented tonality and pace?",
  "hold_usage": "Did the agent use hold properly to avoid dead air?",
  "audio_quality": "Were the agent's audio settings correct and call quality adequate?",
  "screen_recording": "",
  "pre_send_intro": "Did the agent send their name and 'Landing' before sending links, options, or pricing breakdowns?",
};

// ── BRIDGE: legacy compatibility (DROP WITH PhaseTwo §4.6) ───
// `CONFIG.COL` describes the score-destination tab layout (FR1
// for MS, Scores for Sales). Used by QAEntry's FR1-shape
// constructor on the legacy doPost fallback path.
CONFIG.COL = {
  TIMESTAMP:     1,
  MANAGER_EMAIL: 22,
  AGENT_NAME:    2,
  AGENT_EMAIL:   -1,
  DIALPAD_LINK:  0,
  OVERALL_SCORE: 24,
  STRENGTHS:     23,
  IMPROVEMENTS:  23,
};

// Non-MS teams have no legacy production state; legacy
// doPost path is unreachable. Empty stubs avoid
// ReferenceError if it accidentally fires.
CONFIG.FORM_AI_COL = {};
CONFIG.HISTORY_COL = {};
CONFIG.HISTORY_EXTENDED_LAYOUT = [];
