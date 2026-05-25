/**
 * QA Automation — QAEntry
 *
 * Data-model class that wraps a single Analyst_History row. Built via
 * the static `QAEntry.fromHistoryRow(row)` factory only — there is no
 * usable instance constructor (Apps Script doesn't allow private
 * constructors, but no caller invokes `new QAEntry(...)` directly).
 *
 * Read positions come from CONFIG.HISTORY_LAYOUT (auto-generated from
 * the team's HistoryLayout(N)). Keys for numericScores / binaryChecks /
 * aiReasoning / aiConfidence come from CONFIG.NUMERIC_CATEGORIES /
 * BINARY_CATEGORIES so the email cards (which iterate the same arrays)
 * find every value.
 */

class QAEntry {

  /**
   * Constructs a QAEntry from an Analyst_History row.
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

  /** @private
   * Returns:
   *   - a number when the cell holds a numeric value
   *   - null when the cell holds the explicit NA sentinel
   *     ("Not Applicable", emitted by the Python backend for sections
   *     whose team config has `na_applicable: true`)
   *   - 0 when the cell is empty or otherwise unscored, preserving the
   *     historic fallback for legacy rows that don't carry an explicit NA.
   *
   * Null is the load-bearing return — ScoreCard.render branches on it to
   * paint a neutral "N/A" row instead of a red 0/5. Numeric N/A and
   * "unscored" used to be indistinguishable here; the sentinel split is
   * what the email-side fix in PR #34's follow-up depends on.
   */
  static _parseNumberStatic(val) {
    if (QAEntry._isNaSentinel(val)) return null;
    var n = parseFloat(val);
    return isNaN(n) ? 0 : n;
  }

  /** @private
   * Returns:
   *   - true  on "Yes" / "Y"
   *   - false on "No"  / "N"
   *   - null  on the NA sentinel — ScoreCard renders this as a neutral
   *     "— N/A" row rather than a red ✗ No (which is what the old
   *     `charAt(0) === 'Y'` short-circuit produced for "Not Applicable",
   *     since "Not Applicable".charAt(0) === 'N').
   */
  static _parseYesNoStatic(val) {
    if (QAEntry._isNaSentinel(val)) return null;
    var s = (val || '').toString().trim().toUpperCase();
    if (s.charAt(0) === 'Y') return true;
    if (s.charAt(0) === 'N') return false;
    return false;
  }

  /** @private — detect the "Not Applicable" sentinel from the Python writer. */
  static _isNaSentinel(val) {
    return (val || '').toString().trim() === NA_DISPLAY_LABEL;
  }
}

/**
 * Display string the Python backend writes for an explicit N/A cell
 * (see backend/services/sheets_service.py:YN_DISPLAY["NA"]). Match exactly
 * to avoid drift — every team's Python writer uses this same constant.
 */
var NA_DISPLAY_LABEL = 'Not Applicable';
