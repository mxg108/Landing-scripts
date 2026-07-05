/**
 * QA Automation — Config (auto-generated)
 *
 * Team: Member Support (member_support)
 * Rubric version: member_support_v2
 *
 * AUTO-GENERATED — DO NOT EDIT.
 * Run: python qa-automation/scripts/build_config.py member_support
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
  N_SECTIONS:         10,
  TOTAL_WIDTH:        43,
  COL_AGENT_NAME:     0,
  COL_AGENT_EMAIL:    1,
  COL_TIMESTAMP:      2,
  COL_EVALUATOR_EMAIL:3,
  COL_DIALPAD_LINK:   4,
  COL_OVERALL_SCORE:  5,
  SCORES_START:       6,
  SCORES_END:         16,
  REASONING_START:    16,
  REASONING_END:      26,
  CONFIDENCE_START:   26,
  CONFIDENCE_END:     36,
  COL_KEY_STRENGTHS:  36,
  COL_OPPORTUNITIES:  37,
  COL_CALL_SUMMARY:   38,
  COL_CALLER_NAME:    39,
  COL_CALLER_PHONE:   40,
  COL_SOURCE:         41,
};

// ── Section partitions ───────────────────────────────────────
// `key` is the section id (also matches history_id when they're equal),
// `historyIdx` is the position in the derived layout's score range.
CONFIG.NUMERIC_CATEGORIES = [
  { key: "greeting", label: "Greeting", historyIdx: 0 },
  { key: "purpose", label: "Purpose of the Call", historyIdx: 2 },
  { key: "matching", label: "Matching the Moment", historyIdx: 3 },
  { key: "process_adherence", label: "Process Adherence", historyIdx: 4 },
  { key: "call_resolution", label: "Call Resolution", historyIdx: 5 },
  { key: "comms", label: "Communication", historyIdx: 6 },
  { key: "efficiency", label: "Efficiency & Call Handling", historyIdx: 7 },
  { key: "human_review_required", label: "Human Review Required", historyIdx: 8 },
];

CONFIG.BINARY_CATEGORIES = [
  { key: "caller_id", label: "Caller ID", historyIdx: 1 },
  { key: "cri", label: "Customer Resolution Indicator", historyIdx: 9 },
];

CONFIG.MANUAL_CATEGORIES = [
  { key: "human_review_required", label: "Human Review Required", historyIdx: 8 },
];

// ── Section labels + rubric prompts ──────────────────────────
CONFIG.SECTION_LABELS = {
  "greeting": "Greeting",
  "identity_validation": "Caller ID",
  "purpose_of_call": "Purpose of the Call",
  "matching_the_moment": "Matching the Moment",
  "process_adherence": "Process Adherence",
  "call_resolution": "Call Resolution",
  "communication": "Communication",
  "efficiency": "Efficiency & Call Handling",
  "human_review_required": "Human Review Required",
  "customer_resolution_indicator": "Customer Resolution Indicator",
};

CONFIG.RUBRIC_QUESTIONS = {
  "greeting": "Did the agent use the institutional greeting, their name and answer immediately?",
  "identity_validation": "Did the agent verify: full name AND DOB (OR last 4 digits of payment method OR last 4 of SSN)?",
  "purpose_of_call": "Did the agent ask relevant questions to understand the issue?",
  "matching_the_moment": "Was tone and pace appropriate to the context of the call?",
  "process_adherence": "Did the agent follow correct process and company policies?",
  "call_resolution": "Was the issue fully resolved or clear resolution path provided?",
  "communication": "Clear, concise, helpful information?",
  "efficiency": "Handled efficiently without unnecessary delays?",
  "human_review_required": "",
  "customer_resolution_indicator": "Did agent summarize result/actions AND ask if there is anything else they can do?",
};