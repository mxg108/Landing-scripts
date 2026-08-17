/**
 * QA Automation — Config (auto-generated)
 *
 * Team: Sofia AI (sofia)
 * Rubric version: sofia_v0
 *
 * AUTO-GENERATED — DO NOT EDIT.
 * Run: python qa-automation/scripts/build_config.py sofia
 *
 * Mutates the CONFIG object declared in Branding.js (loads first
 * alphabetically). Brand colors, email templates, QA goal, and
 * thresholds live in Branding.js and are not touched here.
 */

// ── Sheet names ──────────────────────────────────────────────
CONFIG.HISTORY_SHEET_NAME = "Analyst_History";
CONFIG.MAILS_SHEET_NAME   = "Mails";

// ── Derived row layout (from HistoryLayout(N)) ───────────────
CONFIG.HISTORY_LAYOUT = {
  N_SECTIONS:         6,
  TOTAL_WIDTH:        34,
  COL_AGENT_NAME:     0,
  COL_AGENT_EMAIL:    1,
  COL_TIMESTAMP:      2,
  COL_EVALUATOR_EMAIL:3,
  COL_DIALPAD_LINK:   4,
  COL_OVERALL_SCORE:  5,
  SCORES_START:       6,
  SCORES_END:         12,
  REASONING_START:    12,
  REASONING_END:      18,
  CONFIDENCE_START:   18,
  CONFIDENCE_END:     24,
  COL_KEY_STRENGTHS:  24,
  COL_OPPORTUNITIES:  25,
  COL_CALL_SUMMARY:   26,
  COL_CALLER_NAME:    27,
  COL_CALLER_PHONE:   28,
  COL_SOURCE:         29,
  COL_DISPOSITION:    31,
  COL_AI_CSAT:        32,
  COL_SOP_REFERENCES: 33,
};

// ── Section partitions ───────────────────────────────────────
// `key` is the section id (also matches history_id when they're equal),
// `historyIdx` is the position in the derived layout's score range.
CONFIG.NUMERIC_CATEGORIES = [
  { key: "sop_adherence", label: "SOP Adherence", historyIdx: 0 },
  { key: "accuracy", label: "Accuracy & Policy Truthfulness", historyIdx: 1 },
  { key: "conversational_flow", label: "Human-Likeness: Conversational Flow", historyIdx: 2 },
  { key: "tone_empathy", label: "Human-Likeness: Tone & Empathy", historyIdx: 3 },
  { key: "intent_resolution", label: "Intent Capture & Resolution", historyIdx: 4 },
];

CONFIG.BINARY_CATEGORIES = [
  { key: "escalation_handling", label: "Escalation & Transfer Handling", historyIdx: 5 },
];

CONFIG.MANUAL_CATEGORIES = [];

// ── Section labels + rubric prompts ──────────────────────────
CONFIG.SECTION_LABELS = {
  "sop_adherence": "SOP Adherence",
  "accuracy": "Accuracy & Policy Truthfulness",
  "conversational_flow": "Human-Likeness: Conversational Flow",
  "tone_empathy": "Human-Likeness: Tone & Empathy",
  "intent_resolution": "Intent Capture & Resolution",
  "escalation_handling": "Escalation & Transfer Handling",
};

CONFIG.RUBRIC_QUESTIONS = {
  "sop_adherence": "Did Sofia follow the applicable Landing SOP(s) for the caller's issue \u2014 required steps, correct information, proper process?",
  "accuracy": "Was everything Sofia stated about Landing policy, the member's account, pricing, or process actually true and grounded?",
  "conversational_flow": "Did the conversation flow like a natural human call \u2014 timing, turn-taking, no awkward gaps, interruptions, or robotic loops?",
  "tone_empathy": "Did Sofia's tone, word choice, and emotional responses fit the caller's situation?",
  "intent_resolution": "Did Sofia correctly identify what the caller needed and either resolve it or set up the right next step?",
  "escalation_handling": "When the call needed a human (explicit request, out-of-scope issue, or policy requirement), did Sofia recognize it and hand off correctly?",
};