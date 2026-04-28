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
   * Appends a QAEntry to the Analyst_History sheet, looks up matching
   * AI enrichment in Form Responses AI, writes it to the new row, and
   * mutates `entry` so the email pipeline can render reasoning inline.
   *
   * @param {QAEntry} entry
   */
  append(entry) {
    this.sheet.appendRow(entry.toHistoryRow());

    var dialpadLink = entry.dialpadLink || '';
    Logger.log('[AnalystHistory] append — dialpadLink: "' + dialpadLink + '"');
    if (!dialpadLink) {
      Logger.log('[AnalystHistory] no dialpadLink — skipping enrichment');
      return;
    }

    var enrichment = this._lookupEnrichment(dialpadLink);
    if (!enrichment) {
      Logger.log('[AnalystHistory] no Form AI match for "' + dialpadLink + '"');
      return;
    }

    var lastRow = this.sheet.getLastRow();
    Logger.log('[AnalystHistory] writing enrichment to row ' + lastRow);
    this._writeEnrichment(lastRow, enrichment);
    this._attachToEntry(entry, enrichment);
  }

  /**
   * Attaches AI enrichment to `entry` WITHOUT writing to the sheet.
   * Used by the dry-run / draft path so previewed emails include reasoning.
   *
   * @param {QAEntry} entry
   */
  enrichEntry(entry) {
    if (!entry.dialpadLink) return;
    var enrichment = this._lookupEnrichment(entry.dialpadLink);
    if (enrichment) this._attachToEntry(entry, enrichment);
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
   * Looks up the matching row in Form Responses AI by Dialpad link and
   * returns a structured enrichment object. Pure read — no side effects.
   *
   * @private
   * @param  {string} dialpadLink
   * @return {Object|null} enrichment data, or null if no match / sheet missing
   */
  _lookupEnrichment(dialpadLink) {
    try {
      var formSheet = this.spreadsheet.getSheetByName(CONFIG.FORM_AI_SHEET_NAME);
      if (!formSheet) return null;

      var formData = formSheet.getDataRange().getValues();
      var FAC = CONFIG.FORM_AI_COL;
      var C   = CONFIG.COL;

      // Search for matching Dialpad link in Form Responses AI col P.
      // Strip any "[LONG CALL]" suffix on either side for comparison.
      var matchedRow = null;
      var cleanHistLink = dialpadLink.replace(/\s*\[LONG CALL.*?\]/, '').trim();
      for (var i = 1; i < formData.length; i++) {
        var formLink = (formData[i][FAC.DIALPAD_LINK] || '').toString().trim();
        var cleanFormLink = formLink.replace(/\s*\[LONG CALL.*?\]/, '').trim();
        if (cleanFormLink && cleanFormLink === cleanHistLink) {
          matchedRow = formData[i];
          break;
        }
      }
      if (!matchedRow) return null;

      var s = function(v) { return (v || '').toString(); };

      return {
        keyStrengths: s(matchedRow[C.STRENGTHS]),
        improvements: s(matchedRow[C.IMPROVEMENTS]),
        source:       s(matchedRow[FAC.SOURCE]),
        callSummary:  s(matchedRow[FAC.CALL_SUMMARY]),
        callerName:   s(matchedRow[FAC.CALLER_NAME]),
        callerPhone:  s(matchedRow[FAC.CALLER_PHONE]),
        confidence: {
          greeting:           s(matchedRow[FAC.GREETING_CONF]),
          identityValidation: s(matchedRow[FAC.IDENTITY_CONF]),
          callPurpose:        s(matchedRow[FAC.PURPOSE_CONF]),
          matchMoment:        s(matchedRow[FAC.MATCHING_CONF]),
          processAdherence:   s(matchedRow[FAC.PROCESS_CONF]),
          callResolution:     s(matchedRow[FAC.RESOLUTION_CONF]),
          communication:      s(matchedRow[FAC.COMMUNICATION_CONF]),
          efficiency:         s(matchedRow[FAC.EFFICIENCY_CONF]),
          customerResolution: s(matchedRow[FAC.CUSTOMER_RES_CONF]),
          documentation:      'manual',
        },
        reasoning: {
          greeting:           s(matchedRow[FAC.GREETING_REASON]),
          identityValidation: s(matchedRow[FAC.IDENTITY_REASON]),
          callPurpose:        s(matchedRow[FAC.PURPOSE_REASON]),
          matchMoment:        s(matchedRow[FAC.MATCHING_REASON]),
          processAdherence:   s(matchedRow[FAC.PROCESS_REASON]),
          callResolution:     s(matchedRow[FAC.RESOLUTION_REASON]),
          communication:      s(matchedRow[FAC.COMMUNICATION_REASON]),
          efficiency:         s(matchedRow[FAC.EFFICIENCY_REASON]),
          customerResolution: s(matchedRow[FAC.CUSTOMER_RES_REASON]),
          documentation:      s(matchedRow[FAC.DOC_REASONING]),
        },
      };
    } catch (err) {
      Logger.log('[AnalystHistory] _lookupEnrichment failed: ' + err.message);
      return null;
    }
  }

  /**
   * Writes a previously-fetched enrichment object to the given row's
   * extended columns (Q–AP). Non-fatal on failure.
   *
   * @private
   * @param {number} historyRowNum
   * @param {Object} enrichment — shape returned by _lookupEnrichment()
   */
  _writeEnrichment(historyRowNum, enrichment) {
    try {
      var HC = CONFIG.HISTORY_COL;
      var r  = enrichment.reasoning;
      var c  = enrichment.confidence;

      var extendedValues = [
        enrichment.keyStrengths,    // Q
        enrichment.improvements,    // R
        enrichment.source,          // S
        c.greeting,                 // T
        r.greeting,                 // U
        c.identityValidation,       // V
        r.identityValidation,       // W
        c.callPurpose,              // X
        r.callPurpose,              // Y
        c.matchMoment,              // Z
        r.matchMoment,              // AA
        c.processAdherence,         // AB
        r.processAdherence,         // AC
        c.callResolution,           // AD
        r.callResolution,           // AE
        c.communication,            // AF
        r.communication,            // AG
        c.efficiency,               // AH
        r.efficiency,               // AI
        c.customerResolution,       // AJ
        r.customerResolution,       // AK
        enrichment.callSummary,     // AL
        enrichment.callerName,      // AM
        enrichment.callerPhone,     // AN
        c.documentation,            // AO ('manual')
        r.documentation,            // AP
      ];

      this.sheet.getRange(historyRowNum, HC.KEY_STRENGTHS + 1, 1, extendedValues.length)
        .setValues([extendedValues]);
    } catch (err) {
      Logger.log('[AnalystHistory] _writeEnrichment failed: ' + err.message);
    }
  }

  /** @private — copies enrichment fields onto a QAEntry. */
  _attachToEntry(entry, enrichment) {
    entry.aiReasoning  = enrichment.reasoning;
    entry.aiConfidence = enrichment.confidence;
    entry.callSummary  = enrichment.callSummary;
    entry.callerName   = enrichment.callerName;
    entry.callerPhone  = enrichment.callerPhone;
  }
}
