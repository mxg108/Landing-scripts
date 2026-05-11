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
   * BRIDGE-WINDOW ONLY (Phase C). Used by the legacy `_processRow` path
   * (FR1-shaped input) which doPost falls back to when the payload only
   * carries `rowNumber` (no `historyRowNumber`). Post-cutover this
   * writes a row in the OLD interleaved-extended-layout shape, which no
   * longer matches Analyst_History's new derived layout — the row will
   * land malformed. Acceptable during the bridge window because no FR1
   * direct-form submissions happen in production. Drop with the bridge
   * cleanup (PhaseTwo §4.6).
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
   * Reads from the Phase-2 derived layout: scores live in
   * `[SCORES_START, SCORES_END)`, reasoning in
   * `[REASONING_START, REASONING_END)`, confidence in
   * `[CONFIDENCE_START, CONFIDENCE_END)`. Per-category lookups go via
   * `historyIdx` from CONFIG.NUMERIC_CATEGORIES / BINARY_CATEGORIES so
   * the email cards (which iterate the same arrays) find every value.
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

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /**
   * @private — returns the Analyst_History sheet, creating it with headers
   * if absent. Headers are derived from CONFIG so a new team's first run
   * lays out its sheet to match its rubric automatically.
   */
  _getOrCreateSheet() {
    var sheet = this.spreadsheet.getSheetByName(this.sheetName);
    if (sheet) return sheet;

    sheet = this.spreadsheet.insertSheet(this.sheetName);

    var headers = ['Agent Name', 'Agent Email', 'Timestamp', 'Overall Score'];

    // Numeric + binary score column headers, in toHistoryRow() order
    CONFIG.NUMERIC_CATEGORIES.forEach(function(cat) { headers.push(cat.label); });
    CONFIG.BINARY_CATEGORIES.forEach(function(cat) { headers.push(cat.label); });

    // Trailing fixed metadata columns (always present across teams)
    headers.push('Manager Email');                  // O
    headers.push('Dialpad Link');                   // P
    headers.push('Key Strengths');                  // Q
    headers.push('Opportunities for Improvement'); // R
    headers.push('Source');                         // S

    // Per-section reasoning + caller meta block, ordered by HISTORY_EXTENDED_LAYOUT
    CONFIG.HISTORY_EXTENDED_LAYOUT.forEach(function(slot) {
      if (slot.type === 'meta') {
        headers.push(slot.label);
      } else {
        // 'reasoning' and 'manual' both occupy a Conf+Reason pair on Analyst_History
        headers.push(slot.label + ' Confidence');
        headers.push(slot.label + ' Reasoning');
      }
    });

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
   * The per-section confidence/reasoning columns are walked dynamically
   * from CONFIG.HISTORY_EXTENDED_LAYOUT, starting at the column after
   * FORM_AI_COL.SOURCE. Slot semantics on Form Responses AI:
   *   - 'reasoning' → 2 cols (Confidence, Reasoning)
   *   - 'meta'      → 1 col, stored under enrichment[slot.key]
   *   - 'manual'    → 1 col (Reasoning only); confidence synthesized as 'manual'
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

      var enrichment = {
        keyStrengths: s(matchedRow[C.STRENGTHS]),
        improvements: s(matchedRow[C.IMPROVEMENTS]),
        source:       s(matchedRow[FAC.SOURCE]),
        callSummary:  '',
        callerName:   '',
        callerPhone:  '',
        confidence:   {},
        reasoning:    {},
      };

      // Walk the layout. Form AI extended block begins at SOURCE+1 (col R for MS).
      var col = FAC.SOURCE + 1;
      CONFIG.HISTORY_EXTENDED_LAYOUT.forEach(function(slot) {
        if (slot.type === 'reasoning') {
          enrichment.confidence[slot.key] = s(matchedRow[col]);
          enrichment.reasoning[slot.key]  = s(matchedRow[col + 1]);
          col += 2;
        } else if (slot.type === 'meta') {
          enrichment[slot.key] = s(matchedRow[col]);
          col += 1;
        } else if (slot.type === 'manual') {
          enrichment.confidence[slot.key] = 'manual';
          enrichment.reasoning[slot.key]  = s(matchedRow[col]);
          col += 1;
        }
      });

      return enrichment;
    } catch (err) {
      Logger.log('[AnalystHistory] _lookupEnrichment failed: ' + err.message);
      return null;
    }
  }

  /**
   * Writes a previously-fetched enrichment object to the given row's
   * extended columns. Non-fatal on failure.
   *
   * Column order is driven by CONFIG.HISTORY_EXTENDED_LAYOUT, starting at
   * KEY_STRENGTHS. On Analyst_History both 'reasoning' and 'manual' slots
   * occupy a Conf+Reason pair (the 'manual' confidence is the literal
   * string 'manual' set by _lookupEnrichment).
   *
   * @private
   * @param {number} historyRowNum
   * @param {Object} enrichment — shape returned by _lookupEnrichment()
   */
  _writeEnrichment(historyRowNum, enrichment) {
    try {
      var HC = CONFIG.HISTORY_COL;

      var values = [
        enrichment.keyStrengths,    // Q
        enrichment.improvements,    // R
        enrichment.source,          // S
      ];

      CONFIG.HISTORY_EXTENDED_LAYOUT.forEach(function(slot) {
        if (slot.type === 'reasoning' || slot.type === 'manual') {
          values.push(enrichment.confidence[slot.key] || '');
          values.push(enrichment.reasoning[slot.key]  || '');
        } else if (slot.type === 'meta') {
          values.push(enrichment[slot.key] || '');
        }
      });

      this.sheet.getRange(historyRowNum, HC.KEY_STRENGTHS + 1, 1, values.length)
        .setValues([values]);
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
