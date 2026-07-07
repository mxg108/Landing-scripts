/**
 * QA Automation — Config (auto-generated)
 *
 * Team: Sales (sales)
 * Rubric version: sales_v2
 *
 * AUTO-GENERATED — DO NOT EDIT.
 * Run: python qa-automation/scripts/build_config.py sales
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
  N_SECTIONS:         15,
  TOTAL_WIDTH:        58,
  COL_AGENT_NAME:     0,
  COL_AGENT_EMAIL:    1,
  COL_TIMESTAMP:      2,
  COL_EVALUATOR_EMAIL:3,
  COL_DIALPAD_LINK:   4,
  COL_OVERALL_SCORE:  5,
  SCORES_START:       6,
  SCORES_END:         21,
  REASONING_START:    21,
  REASONING_END:      36,
  CONFIDENCE_START:   36,
  CONFIDENCE_END:     51,
  COL_KEY_STRENGTHS:  51,
  COL_OPPORTUNITIES:  52,
  COL_CALL_SUMMARY:   53,
  COL_CALLER_NAME:    54,
  COL_CALLER_PHONE:   55,
  COL_SOURCE:         56,
};

// ── Section partitions ───────────────────────────────────────
// `key` is the section id (also matches history_id when they're equal),
// `historyIdx` is the position in the derived layout's score range.
CONFIG.NUMERIC_CATEGORIES = [
  { key: "move_reason", label: "Discover & Personalize the Move Reason", historyIdx: 2 },
  { key: "timeline_housing", label: "Timeline & Housing Needs", historyIdx: 4 },
  { key: "landing_guarantee", label: "Landing Guarantee (Tour Objection)", historyIdx: 5 },
  { key: "pricing", label: "Pricing Breakdown", historyIdx: 6 },
  { key: "fit_confirmation", label: "Confirmation of Fit", historyIdx: 7 },
  { key: "objection_handling", label: "Objection Handling", historyIdx: 8 },
  { key: "client_experience", label: "Client Experience", historyIdx: 14 },
];

CONFIG.BINARY_CATEGORIES = [
  { key: "greeting", label: "Greeting", historyIdx: 0 },
  { key: "stay_type", label: "Personal or COHO Stay", historyIdx: 1 },
  { key: "landing_intro", label: "First Time Hearing About Landing", historyIdx: 3 },
  { key: "urgency", label: "Urgency", historyIdx: 9 },
  { key: "follow_up", label: "Follow-up", historyIdx: 10 },
  { key: "potential_booking", label: "Potential Booking (PB)", historyIdx: 11 },
  { key: "notes_mc", label: "Detailed Notes in MC", historyIdx: 12 },
  { key: "contact_shared", label: "Contact Information Shared", historyIdx: 13 },
];

CONFIG.MANUAL_CATEGORIES = [
  { key: "potential_booking", label: "Potential Booking (PB)", historyIdx: 11 },
  { key: "notes_mc", label: "Detailed Notes in MC", historyIdx: 12 },
];

// ── Section labels + rubric prompts ──────────────────────────
CONFIG.SECTION_LABELS = {
  "greeting": "Greeting",
  "stay_type": "Personal or COHO Stay",
  "move_reason": "Discover & Personalize the Move Reason",
  "landing_intro": "First Time Hearing About Landing",
  "timeline_housing": "Timeline & Housing Needs",
  "landing_guarantee": "Landing Guarantee (Tour Objection)",
  "pricing": "Pricing Breakdown",
  "fit_confirmation": "Confirmation of Fit",
  "objection_handling": "Objection Handling",
  "urgency": "Urgency",
  "follow_up": "Follow-up",
  "potential_booking": "Potential Booking (PB)",
  "notes_mc": "Detailed Notes in MC",
  "contact_shared": "Contact Information Shared",
  "client_experience": "Client Experience",
};

CONFIG.RUBRIC_QUESTIONS = {
  "greeting": "Did the agent say their name, mention 'Landing', and use or confirm the lead's name during the opening?",
  "stay_type": "Did the agent ask whether this would be a personal or COHO stay at any point during the call?",
  "move_reason": "Did the agent discover the reason for the move, respond with empathy, and weave it through the call?",
  "landing_intro": "Did the agent ask if this was the lead's first time hearing about Landing and explain what Landing does?",
  "timeline_housing": "Did the agent explore timeline and housing needs, probe for flexibility, and use it to recommend the right product?",
  "landing_guarantee": "After a tour request, did the agent explain the Landing Guarantee correctly, step-by-step, using the correct name?",
  "pricing": "Did the agent deliver a clear, confident pricing breakdown tailored to the lead's situation?",
  "fit_confirmation": "Did the agent ask whether the options presented felt like a good fit and use the answer to guide next steps?",
  "objection_handling": "Did the agent distinguish real concerns from smokescreens, resolve them with Landing's benefits, and confirm next steps?",
  "urgency": "Did the agent mention that prices can change and/or that the apartment could be taken online?",
  "follow_up": "Did the agent set up a follow-up with a specific medium and a specific time before ending the call?",
  "potential_booking": "",
  "notes_mc": "",
  "contact_shared": "Did the agent send their contact information to the lead at some point during the interaction?",
  "client_experience": "Across the whole call, did the lead feel genuinely heard and cared for rather than processed through a script?",
};