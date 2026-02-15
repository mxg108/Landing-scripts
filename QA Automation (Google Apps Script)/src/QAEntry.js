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

  /** Returns a flat object suitable for writing to the Analyst_History sheet. */
  toHistoryRow() {
    return [
      this.agentName,
      this.agentEmail,
      this.timestamp,
      this.overallScore,
      this.numericScores.greeting,
      this.numericScores.callPurpose,
      this.numericScores.matchMoment,
      this.numericScores.processAdherence,
      this.numericScores.callResolution,
      this.numericScores.communication,
      this.numericScores.efficiency,
      this.numericScores.documentation,
      this.binaryChecks.identityValidation  ? 'Y' : 'N',
      this.binaryChecks.customerResolution  ? 'Y' : 'N',
      this.managerEmail,
    ];
  }

  /**
   * Returns the color hex for a given numeric score based on thresholds.
   * @param {number} score
   * @return {string} hex color
   */
  static colorForScore(score) {
    if (score >= CONFIG.THRESHOLDS.HIGH) return CONFIG.COLORS.ACCENT_BLUE;
    if (score >= CONFIG.THRESHOLDS.MID)  return CONFIG.COLORS.AMBER;
    return CONFIG.COLORS.RED;
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /** @private */
  _parseNumber(val) {
    var n = parseFloat(val);
    return isNaN(n) ? 0 : n;
  }

  /** @private */
  _parseYesNo(val) {
    return (val || '').toString().trim().toUpperCase().charAt(0) === 'Y';
  }
}
