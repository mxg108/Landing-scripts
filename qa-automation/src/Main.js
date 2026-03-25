/**
 * QA Automation — Main
 * Landing QA System v1.1.0
 *
 * Entry points:
 *   • onFormSubmit(e)  — installable trigger, fires on each QA form submission
 *   • onOpen()         — adds a custom menu to the spreadsheet
 *   • processLatestRow — manual fallback to process the last row
 *   • createDraftForLatest — creates a draft instead of sending (dry-run)
 */

// ══════════════════════════════════════════════════════════════════
// Trigger & Menu
// ══════════════════════════════════════════════════════════════════

/**
 * Installable trigger — runs each time a form response is submitted.
 *
 * @param {Object} e — the event object from the form-submit trigger
 */
function onFormSubmit(e) {
  // Sanity-check: log every element in e.values with its index and column letter.
  // Useful for confirming which columns the form populates vs. which are formula-driven.
  Logger.log('=== onFormSubmit e.values dump (%s elements) ===', e.values.length);
  for (var i = 0; i < e.values.length; i++) {
    Logger.log('  [%s] col %s: "%s"', i, String.fromCharCode(65 + i), e.values[i]);
  }
  Logger.log('=== end of e.values dump ===');

  try {
    // e.range.getRow() tells us exactly which row the form just appended.
    // We sleep briefly to let ARRAYFORMULAs (overallScore col Q, agentEmail col V)
    // finish computing before we re-read the full row from the live sheet.
    var rowNum = e.range.getRow();
    Utilities.sleep(3000);

    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(CONFIG.QA_SHEET_NAME);
    var row   = sheet.getRange(rowNum, 1, 1, sheet.getLastColumn()).getValues()[0];

    _processRow(row);
  } catch (err) {
    _handleError(err, 'onFormSubmit');
  }
}

/**
 * Adds a custom menu to the spreadsheet for manual operations.
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('QA Automation')
    .addItem('Send email for latest QA',      'processLatestRow')
    .addItem('Create draft for latest QA',     'createDraftForLatest')
    .addSeparator()
    .addItem('Rebuild Analyst_History sheet',   'rebuildHistory')
    .addToUi();
}

// ══════════════════════════════════════════════════════════════════
// Manual entry points
// ══════════════════════════════════════════════════════════════════

/** Processes (sends email for) the most recent row in the QA Sheet. */
function processLatestRow() {
  try {
    var row = _getLatestRow();
    _processRow(row);
    SpreadsheetApp.getUi().alert('QA email sent successfully.');
  } catch (err) {
    _handleError(err, 'processLatestRow');
  }
}

/** Creates a Gmail draft for the most recent row (dry-run / review). */
function createDraftForLatest() {
  try {
    var row   = _getLatestRow();
    var entry = new QAEntry(row);

    var ss      = SpreadsheetApp.getActiveSpreadsheet();
    var history = new AnalystHistory(ss);

    var pastEntries = history.getHistory(entry.agentName);

    var scoreCard       = new ScoreCard(entry);
    var feedbackCard    = new FeedbackCard(entry);
    var progressionCard = new ProgressionCard(entry, pastEntries);
    var renderer        = new HtmlRenderer(entry, scoreCard, feedbackCard, progressionCard);

    var sender = new EmailSender(entry);
    sender.createDraft(renderer.renderEmail());

    SpreadsheetApp.getUi().alert(
      'Draft created in Gmail for ' + entry.agentName + '.\nCheck your Drafts folder.'
    );
  } catch (err) {
    _handleError(err, 'createDraftForLatest');
  }
}

/**
 * Rebuilds the Analyst_History sheet from all existing QA Sheet rows.
 * Useful after first deployment or if the history sheet was deleted.
 */
function rebuildHistory() {
  try {
    var ss    = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(CONFIG.QA_SHEET_NAME);
    if (!sheet) throw new Error('QA Sheet "' + CONFIG.QA_SHEET_NAME + '" not found.');

    var data    = sheet.getDataRange().getValues();
    var history = new AnalystHistory(ss);

    // Clear existing history data (keep header)
    var hSheet = history.sheet;
    if (hSheet.getLastRow() > 1) {
      hSheet.getRange(2, 1, hSheet.getLastRow() - 1, hSheet.getLastColumn()).clearContent();
    }

    // Re-populate from row 2 onward (skip header)
    var count = 0;
    for (var i = 1; i < data.length; i++) {
      var row = data[i];
      if (!row[CONFIG.COL.AGENT_NAME]) continue;  // skip empty rows
      var entry = new QAEntry(row);
      history.append(entry);
      count++;
    }

    SpreadsheetApp.getUi().alert(
      'Analyst_History rebuilt with ' + count + ' entries.'
    );
  } catch (err) {
    _handleError(err, 'rebuildHistory');
  }
}

// ══════════════════════════════════════════════════════════════════
// Core pipeline
// ══════════════════════════════════════════════════════════════════

/**
 * Core processing pipeline — shared by trigger and manual entry points.
 *
 * @param {Array} row — values array for a single QA Sheet row
 * @private
 */
function _processRow(row) {
  // 1. Parse the row into a structured QAEntry
  var entry = new QAEntry(row);

  // 1b. Column V (agentEmail) is ARRAYFORMULA-driven and may be empty when the
  //     trigger fires. Always resolve from the Mails sheet as the source of truth.
  entry.agentEmail = _lookupAgentEmail(entry.agentName);

  // 2. Open spreadsheet and history manager
  var ss      = SpreadsheetApp.getActiveSpreadsheet();
  var history = new AnalystHistory(ss);

  // 3. Retrieve past entries BEFORE appending the current one
  var pastEntries = history.getHistory(entry.agentName);

  // 4. Append current entry to history
  history.append(entry);

  // 5. Include current entry in the progression display
  //    (prepend to front since history is newest-first)
  var allEntries = [
    {
      agentName:    entry.agentName,
      agentEmail:   entry.agentEmail,
      timestamp:    entry.timestamp,
      overallScore: entry.overallScore,
    }
  ].concat(pastEntries);

  // 6. Build HTML cards
  var scoreCard       = new ScoreCard(entry);
  var feedbackCard    = new FeedbackCard(entry);
  var progressionCard = new ProgressionCard(entry, allEntries);

  // 7. Render full email
  var renderer = new HtmlRenderer(entry, scoreCard, feedbackCard, progressionCard);
  var htmlBody = renderer.renderEmail();

  // 8. Send
  var sender = new EmailSender(entry);
  sender.send(htmlBody);
}

// ══════════════════════════════════════════════════════════════════
// Utilities
// ══════════════════════════════════════════════════════════════════

/**
 * Reads the last data row from the QA Sheet.
 * @private
 * @return {Array}
 */
function _getLatestRow() {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(CONFIG.QA_SHEET_NAME);
  if (!sheet) throw new Error('QA Sheet "' + CONFIG.QA_SHEET_NAME + '" not found.');

  // Use Timestamp column (A) to find the real last data row.
  // sheet.getLastRow() is unreliable when ARRAYFORMULAs extend empty rows.
  var timestamps = sheet.getRange('A:A').getValues();
  var lastRow = timestamps.length;
  while (lastRow > 1 && !timestamps[lastRow - 1][0]) lastRow--;

  if (lastRow < 2) throw new Error('No data rows found in the QA Sheet.');

  return sheet.getRange(lastRow, 1, 1, sheet.getLastColumn()).getValues()[0];
}

/**
 * Looks up an agent's email from the "Mails" sheet by agent name.
 * This is the source of truth for agent emails — more reliable than the
 * ARRAYFORMULA in column V, which may not resolve before the trigger fires.
 *
 * Expects the Mails sheet to have:
 *   Column A — Agent name (must match QA form exactly)
 *   Column B — Agent email
 *
 * @private
 * @param {string} agentName
 * @return {string} email address, or empty string if not found
 */
function _lookupAgentEmail(agentName) {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(CONFIG.MAILS_SHEET_NAME);
  if (!sheet) {
    Logger.log('_lookupAgentEmail: sheet "%s" not found.', CONFIG.MAILS_SHEET_NAME);
    return '';
  }
  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {  // row 0 is the header
    if (data[i][0] === agentName) {
      Logger.log('_lookupAgentEmail: found "%s" → %s', agentName, data[i][1]);
      return data[i][1];
    }
  }
  Logger.log('_lookupAgentEmail: no match found for "%s"', agentName);
  return '';
}

/**
 * Centralized error handler — logs and (if UI available) alerts.
 * @private
 * @param {Error}  err
 * @param {string} context — name of the calling function
 */
function _handleError(err, context) {
  Logger.log('[QA Automation ERROR in %s] %s\n%s', context, err.message, err.stack);
  try {
    SpreadsheetApp.getUi().alert(
      'QA Automation Error (' + context + '):\n' + err.message
    );
  } catch (_) {
    // UI not available (running from trigger) — log only
  }
}
