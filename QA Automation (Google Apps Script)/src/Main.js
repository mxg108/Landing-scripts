/**
 * QA Automation — Main
 * Landing QA System v1.0.0
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
 * Set this up via:  Triggers → Add Trigger → onFormSubmit → From spreadsheet → On form submit
 *
 * @param {Object} e — the event object from the form-submit trigger
 */
function onFormSubmit(e) {
  try {
    var row = e.values;  // flat array of the submitted row's values
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

  var lastRow = sheet.getLastRow();
  if (lastRow < 2) throw new Error('No data rows found in the QA Sheet.');

  return sheet.getRange(lastRow, 1, 1, sheet.getLastColumn()).getValues()[0];
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
