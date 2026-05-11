/**
 * QA Automation — QAEntry
 *
 * Data-model class that wraps a single QA form-response row.
 * Provides structured access to scores, feedback, and metadata.
 */

class QAEntry {
  /**
   * @param {Array} row  — a single row from the QA Sheet (values array, 0-indexed)
   */
  constructor(row) {
    const C = CONFIG.COL;

    // ── Metadata ────────────────────────────────────────────────
    this.timestamp    = new Date(row[C.TIMESTAMP]);
    this.managerEmail = (row[C.MANAGER_EMAIL] || '').toString().trim();
    this.agentName    = (row[C.AGENT_NAME]    || '').toString().trim();
    this.agentEmail   = (row[C.AGENT_EMAIL]   || '').toString().trim();
    this.dialpadLink  = (row[C.DIALPAD_LINK]  || '').toString().trim();

    // ── Overall score (auto-calculated by the Sheet) ────────────
    this.overallScore = this._parseNumber(row[C.OVERALL_SCORE]);

    // ── Numeric scores (1-5) ────────────────────────────────────
    this.numericScores = {};
    CONFIG.NUMERIC_CATEGORIES.forEach(function(cat) {
      this.numericScores[cat.key] = this._parseNumber(row[cat.col]);
    }.bind(this));

    // ── Binary checks (Y/N → boolean) ───────────────────────────
    this.binaryChecks = {};
    CONFIG.BINARY_CATEGORIES.forEach(function(cat) {
      this.binaryChecks[cat.key] = this._parseYesNo(row[cat.col]);
    }.bind(this));

    // ── Qualitative feedback ────────────────────────────────────
    this.strengths    = (row[C.STRENGTHS]    || '').toString().trim();
    this.improvements = (row[C.IMPROVEMENTS] || '').toString().trim();

    // ── AI enrichment slots (populated post-construction by
    //    AnalystHistory.append() / .enrichEntry() via Form Responses AI) ──
    this.aiReasoning  = {};
    this.aiConfidence = {};
    this.callSummary  = '';
    this.callerName   = '';
    this.callerPhone  = '';
  }

  // ────────────────────────────────────────────────────────────────
  // Static factories
  // ────────────────────────────────────────────────────────────────

  /**
   * Constructs a QAEntry from an Analyst_History row (post-Phase-2
   * layout). Used by Main.js doPost when Python's Stage 4 has already
   * populated the row with scores + reasoning + confidence + feedback +
   * caller meta.
   *
   * Row positions come from CONFIG.HISTORY_LAYOUT (auto-generated from
   * the team's HistoryLayout(N)). Keys for numericScores / binaryChecks
   * / aiReasoning / aiConfidence come from CONFIG.NUMERIC_CATEGORIES /
   * BINARY_CATEGORIES so the email cards (which iterate the same arrays)
   * find every value.
   *
   * @param  {Array} row — values array read from the Analyst_History row
   * @return {QAEntry}
   */
  static fromHistoryRow(row) {
    var L = CONFIG.HISTORY_LAYOUT;
    var entry = Object.create(QAEntry.prototype);

    // ── Metadata ────────────────────────────────────────────────
    entry.agentName    = (row[L.COL_AGENT_NAME]      || '').toString().trim();
    entry.agentEmail   = (row[L.COL_AGENT_EMAIL]     || '').toString().trim();
    entry.timestamp    = new Date(row[L.COL_TIMESTAMP]);
    entry.managerEmail = (row[L.COL_EVALUATOR_EMAIL] || '').toString().trim();
    entry.dialpadLink  = (row[L.COL_DIALPAD_LINK]    || '').toString().trim();
    entry.overallScore = QAEntry._parseNumberStatic(row[L.COL_OVERALL_SCORE]);

    // ── Score / reasoning / confidence per section ──────────────
    entry.numericScores = {};
    entry.binaryChecks  = {};
    entry.aiReasoning   = {};
    entry.aiConfidence  = {};

    CONFIG.NUMERIC_CATEGORIES.forEach(function(cat) {
      entry.numericScores[cat.key] = QAEntry._parseNumberStatic(row[L.SCORES_START + cat.historyIdx]);
      entry.aiReasoning[cat.key]   = (row[L.REASONING_START  + cat.historyIdx] || '').toString();
      entry.aiConfidence[cat.key]  = (row[L.CONFIDENCE_START + cat.historyIdx] || '').toString();
    });

    CONFIG.BINARY_CATEGORIES.forEach(function(cat) {
      entry.binaryChecks[cat.key] = QAEntry._parseYesNoStatic(row[L.SCORES_START + cat.historyIdx]);
      entry.aiReasoning[cat.key]  = (row[L.REASONING_START  + cat.historyIdx] || '').toString();
      entry.aiConfidence[cat.key] = (row[L.CONFIDENCE_START + cat.historyIdx] || '').toString();
    });

    // ── Feedback + caller meta ──────────────────────────────────
    entry.strengths    = (row[L.COL_KEY_STRENGTHS] || '').toString().trim();
    // Legacy field name: code downstream reads `entry.improvements`. The
    // new layout stores the value at COL_OPPORTUNITIES; the rename is a
    // separate Tier 2 follow-up.
    entry.improvements = (row[L.COL_OPPORTUNITIES] || '').toString().trim();
    entry.callSummary  = (row[L.COL_CALL_SUMMARY]  || '').toString().trim();
    entry.callerName   = (row[L.COL_CALLER_NAME]   || '').toString().trim();
    entry.callerPhone  = (row[L.COL_CALLER_PHONE]  || '').toString().trim();

    return entry;
  }

  // ────────────────────────────────────────────────────────────────
  // Public helpers
  // ────────────────────────────────────────────────────────────────

  /** Formatted date string for display (e.g. "Feb 15, 2026"). */
  get formattedDate() {
    return Utilities.formatDate(
      this.timestamp,
      Session.getScriptTimeZone(),
      'MMM dd, yyyy'
    );
  }

  /**
   * Returns a flat object suitable for writing to the Analyst_History sheet.
   * Numeric and binary slots are emitted in CONFIG.NUMERIC_CATEGORIES /
   * BINARY_CATEGORIES order so each team's rubric drives the column layout.
   */
  toHistoryRow() {
    var row = [
      this.agentName,        // A
      this.agentEmail,       // B
      this.timestamp,        // C
      this.overallScore,     // D
    ];

    // Numeric scores (1–5) in rubric order
    CONFIG.NUMERIC_CATEGORIES.forEach(function(cat) {
      row.push(this.numericScores[cat.key]);
    }, this);

    // Binary checks (Y/N) in rubric order
    CONFIG.BINARY_CATEGORIES.forEach(function(cat) {
      row.push(this.binaryChecks[cat.key] ? 'Y' : 'N');
    }, this);

    row.push(this.managerEmail);  // O
    row.push(this.dialpadLink);   // P  (Dialpad link — used as lookup key for AI reasoning)

    return row;
  }

  /**
   * Returns color hex for a 1-5 category score.
   * @param {number} score
   * @return {string} hex color
   */
  static colorForScore(score) {
    if (score >= CONFIG.THRESHOLDS.CATEGORY_HIGH) return CONFIG.COLORS.GREEN;
    if (score >= CONFIG.THRESHOLDS.CATEGORY_MID)  return CONFIG.COLORS.AMBER;
    return CONFIG.COLORS.RED;
  }

  /**
   * Returns color hex for an overall 0-100 score.
   * @param {number} score
   * @return {string} hex color
   */
  static colorForOverallScore(score) {
    if (score >= 100)                              return CONFIG.COLORS.GOLD;
    if (score >= CONFIG.THRESHOLDS.OVERALL_HIGH)   return CONFIG.COLORS.GREEN;
    if (score >= CONFIG.THRESHOLDS.OVERALL_MID)    return CONFIG.COLORS.AMBER;
    return CONFIG.COLORS.RED;
  }

  /**
   * Extracts a human-readable name from an email address.
   * "john.doe@company.com" → "John Doe"
   * @param {string} email
   * @return {string}
   */
  static managerNameFromEmail(email) {
    var local = (email || '').split('@')[0] || '';
    return local.split(/[._-]/).map(function(part) {
      return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
    }).join(' ');
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /** @private */
  _parseNumber(val) {
    return QAEntry._parseNumberStatic(val);
  }

  /** @private */
  _parseYesNo(val) {
    return QAEntry._parseYesNoStatic(val);
  }

  /** @private */
  static _parseNumberStatic(val) {
    var n = parseFloat(val);
    return isNaN(n) ? 0 : n;
  }

  /** @private */
  static _parseYesNoStatic(val) {
    return (val || '').toString().trim().toUpperCase().charAt(0) === 'Y';
  }
}
