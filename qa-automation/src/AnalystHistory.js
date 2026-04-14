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

    // Enrich with AI reasoning from Form Responses AI (if available)
    var dialpadLink = entry.dialpadLink || '';
    Logger.log('[AnalystHistory] append — dialpadLink: "' + dialpadLink + '"');
    if (dialpadLink) {
      var lastRow = this.sheet.getLastRow();
      Logger.log('[AnalystHistory] calling _enrichFromFormResponsesAI for row ' + lastRow);
      this._enrichFromFormResponsesAI(lastRow, dialpadLink);
    } else {
      Logger.log('[AnalystHistory] no dialpadLink — skipping enrichment');
    }
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
          dialpadLink:        (row[HC.DIALPAD_LINK]  || '').toString().trim(),
          keyStrengths:       (row[HC.KEY_STRENGTHS]  || '').toString().trim(),
          improvements:       (row[HC.IMPROVEMENTS]   || '').toString().trim(),
          source:             (row[HC.SOURCE]          || '').toString().trim(),
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
      'Dialpad Link',                    // P
      'Key Strengths',                   // Q
      'Opportunities for Improvement',   // R
      'Source',                          // S
      'Greeting Confidence',             // T
      'Greeting Reasoning',              // U
      'Identity Validation Confidence',  // V
      'Identity Validation Reasoning',   // W
      'Purpose of Call Confidence',      // X
      'Purpose of Call Reasoning',       // Y
      'Matching the Moment Confidence',  // Z
      'Matching the Moment Reasoning',   // AA
      'Process Adherence Confidence',    // AB
      'Process Adherence Reasoning',     // AC
      'Call Resolution Confidence',      // AD
      'Call Resolution Reasoning',       // AE
      'Communication Confidence',        // AF
      'Communication Reasoning',         // AG
      'Efficiency Confidence',           // AH
      'Efficiency Reasoning',            // AI
      'Customer Resolution Confidence',  // AJ
      'Customer Resolution Reasoning',   // AK
      'Call Summary',                    // AL
      'Caller Name',                     // AM
      'Caller Phone',                    // AN
      'Documentation Confidence',        // AO
      'Documentation Reasoning',         // AP
    ];
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.getRange(1, 1, 1, headers.length)
      .setFontWeight('bold')
      .setBackground(CONFIG.COLORS.DARK_NAVY)
      .setFontColor(CONFIG.COLORS.WHITE);
    sheet.setFrozenRows(1);
    return sheet;
  }

  /**
   * Looks up the matching row in Form Responses AI by Dialpad link
   * and copies AI reasoning data (strengths, improvements, confidence,
   * reasoning per section) into the extended columns (Q-AK) of the
   * given Analyst_History row.
   *
   * @private
   * @param {number} historyRowNum — the row number in Analyst_History to enrich
   * @param {string} dialpadLink — the Dialpad link to match on
   */
  _enrichFromFormResponsesAI(historyRowNum, dialpadLink) {
    try {
      var formSheet = this.spreadsheet.getSheetByName(CONFIG.FORM_AI_SHEET_NAME);
      if (!formSheet) return;  // Form Responses AI tab doesn't exist — skip silently

      var formData = formSheet.getDataRange().getValues();
      var FAC = CONFIG.FORM_AI_COL;

      // Search for matching Dialpad link in Form Responses AI col P
      var matchedRow = null;
      for (var i = 1; i < formData.length; i++) {
        var formLink = (formData[i][FAC.DIALPAD_LINK] || '').toString().trim();
        // Strip any "[LONG CALL]" suffix for comparison
        var cleanFormLink = formLink.replace(/\s*\[LONG CALL.*?\]/, '').trim();
        var cleanHistLink = dialpadLink.replace(/\s*\[LONG CALL.*?\]/, '').trim();
        if (cleanFormLink && cleanFormLink === cleanHistLink) {
          matchedRow = formData[i];
          break;
        }
      }

      if (!matchedRow) {
        Logger.log('[AnalystHistory] _enrich: no match found for link "' + dialpadLink + '" in ' + formData.length + ' Form AI rows');
        return;
      }
      Logger.log('[AnalystHistory] _enrich: matched! Writing extended cols to row ' + historyRowNum);

      var HC = CONFIG.HISTORY_COL;

      // Build the extended values array for cols Q-AK (indices 16-36)
      var extendedValues = [
        (matchedRow[13] || '').toString(),  // Q: Key Strengths (Form col N)
        (matchedRow[14] || '').toString(),  // R: Improvements (Form col O)
        (matchedRow[FAC.SOURCE] || '').toString(),                  // S: Source
        (matchedRow[FAC.GREETING_CONF] || '').toString(),           // T
        (matchedRow[FAC.GREETING_REASON] || '').toString(),         // U
        (matchedRow[FAC.IDENTITY_CONF] || '').toString(),           // V
        (matchedRow[FAC.IDENTITY_REASON] || '').toString(),         // W
        (matchedRow[FAC.PURPOSE_CONF] || '').toString(),            // X
        (matchedRow[FAC.PURPOSE_REASON] || '').toString(),          // Y
        (matchedRow[FAC.MATCHING_CONF] || '').toString(),           // Z
        (matchedRow[FAC.MATCHING_REASON] || '').toString(),         // AA
        (matchedRow[FAC.PROCESS_CONF] || '').toString(),            // AB
        (matchedRow[FAC.PROCESS_REASON] || '').toString(),          // AC
        (matchedRow[FAC.RESOLUTION_CONF] || '').toString(),         // AD
        (matchedRow[FAC.RESOLUTION_REASON] || '').toString(),       // AE
        (matchedRow[FAC.COMMUNICATION_CONF] || '').toString(),      // AF
        (matchedRow[FAC.COMMUNICATION_REASON] || '').toString(),    // AG
        (matchedRow[FAC.EFFICIENCY_CONF] || '').toString(),         // AH
        (matchedRow[FAC.EFFICIENCY_REASON] || '').toString(),       // AI
        (matchedRow[FAC.CUSTOMER_RES_CONF] || '').toString(),       // AJ
        (matchedRow[FAC.CUSTOMER_RES_REASON] || '').toString(),     // AK
        (matchedRow[FAC.CALL_SUMMARY] || '').toString(),            // AL
        (matchedRow[FAC.CALLER_NAME] || '').toString(),             // AM
        (matchedRow[FAC.CALLER_PHONE] || '').toString(),            // AN
        'manual',                                                   // AO: Documentation Confidence
        (matchedRow[FAC.DOC_REASONING] || '').toString(),           // AP: Documentation Reasoning
      ];

      // Write to Analyst_History cols Q-AN (columns 17-40 in 1-indexed)
      this.sheet.getRange(historyRowNum, HC.KEY_STRENGTHS + 1, 1, extendedValues.length)
        .setValues([extendedValues]);

    } catch (err) {
      // Non-fatal — log but don't block the append
      Logger.log('[AnalystHistory] _enrichFromFormResponsesAI failed: ' + err.message);
    }
  }
}
