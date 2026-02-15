/**
 * QA Automation — AnalystHistory
 *
 * Manages the Analyst_History sheet: creates it if missing,
 * appends new QA entries, and retrieves past entries for an analyst.
 */

class AnalystHistory {
  /**
   * @param {Spreadsheet} spreadsheet — the parent spreadsheet object
   */
  constructor(spreadsheet) {
    this.spreadsheet = spreadsheet;
    this.sheetName   = CONFIG.HISTORY_SHEET_NAME;
    this.sheet       = this._getOrCreateSheet();
  }

  // ────────────────────────────────────────────────────────────────
  // Public API
  // ────────────────────────────────────────────────────────────────

  /**
   * Appends a QAEntry to the Analyst_History sheet.
   * @param {QAEntry} entry
   */
  append(entry) {
    this.sheet.appendRow(entry.toHistoryRow());
  }

  /**
   * Returns the most recent N QAEntry-like objects for a given agent,
   * sorted newest-first.
   *
   * @param  {string} agentName  — value to match against column A
   * @param  {number} [limit]    — max records to return (default CONFIG.EMAIL.MAX_HISTORY)
   * @return {Object[]} array of plain objects with keys matching HISTORY_COL
   */
  getHistory(agentName, limit) {
    limit = limit || CONFIG.EMAIL.MAX_HISTORY;
    var data = this.sheet.getDataRange().getValues();
    var HC   = CONFIG.HISTORY_COL;

    // Skip header row (index 0), filter by agent name
    var matches = [];
    for (var i = 1; i < data.length; i++) {
      var row = data[i];
      if ((row[HC.AGENT_NAME] || '').toString().trim() === agentName) {
        matches.push({
          agentName:          row[HC.AGENT_NAME],
          agentEmail:         row[HC.AGENT_EMAIL],
          timestamp:          new Date(row[HC.TIMESTAMP]),
          overallScore:       parseFloat(row[HC.OVERALL_SCORE]) || 0,
          greeting:           parseFloat(row[HC.GREETING])      || 0,
          callPurpose:        parseFloat(row[HC.CALL_PURPOSE])  || 0,
          matchMoment:        parseFloat(row[HC.MATCH_MOMENT])  || 0,
          processAdherence:   parseFloat(row[HC.PROCESS])       || 0,
          callResolution:     parseFloat(row[HC.RESOLUTION])    || 0,
          communication:      parseFloat(row[HC.COMMUNICATION]) || 0,
          efficiency:         parseFloat(row[HC.EFFICIENCY])     || 0,
          documentation:      parseFloat(row[HC.DOCUMENTATION]) || 0,
          identityValidation: (row[HC.IDENTITY_VAL]  || '').toString().trim().toUpperCase().charAt(0) === 'Y',
          customerResolution: (row[HC.CUSTOMER_RES]  || '').toString().trim().toUpperCase().charAt(0) === 'Y',
          managerEmail:       row[HC.MANAGER_EMAIL],
        });
      }
    }

    // Sort newest first, then trim
    matches.sort(function(a, b) { return b.timestamp - a.timestamp; });
    return matches.slice(0, limit);
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /** @private — returns the Analyst_History sheet, creating it with headers if absent. */
  _getOrCreateSheet() {
    var sheet = this.spreadsheet.getSheetByName(this.sheetName);
    if (sheet) return sheet;

    sheet = this.spreadsheet.insertSheet(this.sheetName);
    var headers = [
      'Agent Name',
      'Agent Email',
      'Timestamp',
      'Overall Score',
      'Greeting',
      'Purpose of the Call',
      'Matching the Moment',
      'Process Adherence',
      'Call Resolution',
      'Communication',
      'Efficiency & Call Handling',
      'Documentation',
      'Identity Validation',
      'Customer Resolution',
      'Manager Email',
    ];
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.getRange(1, 1, 1, headers.length)
      .setFontWeight('bold')
      .setBackground(CONFIG.COLORS.DARK_NAVY)
      .setFontColor(CONFIG.COLORS.WHITE);
    sheet.setFrozenRows(1);
    return sheet;
  }
}
