/**
 * QA Automation — AnalystHistory
 *
 * Reads past entries from the Analyst_History sheet for the email's
 * progression card. The sheet itself is populated by the FastAPI
 * backend (Stage 4 of the scoring pipeline), so this class only reads
 * — it never writes.
 *
 * Row layout comes from CONFIG.HISTORY_LAYOUT (auto-generated from the
 * team's HistoryLayout(N)). Per-category lookups go via `historyIdx`
 * from CONFIG.NUMERIC_CATEGORIES / BINARY_CATEGORIES so the email
 * cards (which iterate the same arrays) find every value.
 */

class AnalystHistory {
  /**
   * @param {Spreadsheet} spreadsheet — the parent spreadsheet object
   */
  constructor(spreadsheet) {
    this.spreadsheet = spreadsheet;
    this.sheetName   = CONFIG.HISTORY_SHEET_NAME;
    this.sheet       = spreadsheet.getSheetByName(this.sheetName);
    if (!this.sheet) {
      throw new Error('Sheet "' + this.sheetName + '" not found.');
    }
  }

  /**
   * Returns the most recent N QAEntry-like objects for a given agent,
   * sorted newest-first.
   *
   * @param  {string} agentName  — value to match against column A
   * @param  {number} [limit]    — max records to return (default CONFIG.EMAIL.MAX_HISTORY)
   * @return {Object[]} plain objects with overallScore + per-category fields
   */
  getHistory(agentName, limit) {
    limit = limit || CONFIG.EMAIL.MAX_HISTORY;
    var data = this.sheet.getDataRange().getValues();
    var L    = CONFIG.HISTORY_LAYOUT;

    var matches = [];
    for (var i = 1; i < data.length; i++) {
      var row = data[i];
      if ((row[L.COL_AGENT_NAME] || '').toString().trim() !== agentName) continue;

      var entry = {
        agentName:    row[L.COL_AGENT_NAME],
        agentEmail:   row[L.COL_AGENT_EMAIL],
        timestamp:    new Date(row[L.COL_TIMESTAMP]),
        managerEmail: (row[L.COL_EVALUATOR_EMAIL] || '').toString().trim(),
        dialpadLink:  (row[L.COL_DIALPAD_LINK]    || '').toString().trim(),
        overallScore: parseFloat(row[L.COL_OVERALL_SCORE]) || 0,
        numericScores: {},
        binaryChecks:  {},
        // Tier 2.1 follow-up: standardize on `opportunities` everywhere.
        keyStrengths: (row[L.COL_KEY_STRENGTHS] || '').toString().trim(),
        improvements: (row[L.COL_OPPORTUNITIES] || '').toString().trim(),
        source:       (row[L.COL_SOURCE]        || '').toString().trim(),
      };

      CONFIG.NUMERIC_CATEGORIES.forEach(function(cat) {
        var val = parseFloat(row[L.SCORES_START + cat.historyIdx]) || 0;
        entry.numericScores[cat.key] = val;
        entry[cat.key] = val;  // top-level alias, kept for legacy callers
      });
      CONFIG.BINARY_CATEGORIES.forEach(function(cat) {
        var raw = (row[L.SCORES_START + cat.historyIdx] || '').toString().trim().toUpperCase();
        var passed = raw.charAt(0) === 'Y';
        entry.binaryChecks[cat.key] = passed;
        entry[cat.key] = passed;
      });

      matches.push(entry);
    }

    matches.sort(function(a, b) { return b.timestamp - a.timestamp; });
    return matches.slice(0, limit);
  }
}
