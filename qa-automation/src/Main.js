/**
 * QA Automation — Main
 *
 * Single entry point for the email pipeline:
 *
 *   doPost(e) — invoked by the FastAPI backend after a manager approves
 *               a scorecard. Reads the populated Analyst_History row that
 *               backend Stage 4 wrote, builds a QAEntry, and dispatches
 *               the email via _processHistoryRow.
 *
 * The full pre-email pipeline (audio → Gemini scoring → analyst edits →
 * destination tab → readback → Analyst_History row) lives in the backend.
 * This script is a dumb email-dispatcher.
 *
 * Payload shape:
 *   { "historyRowNumber": <int> }
 *
 * Response:
 *   { "status": "ok"|"error", "message": "..." }
 */

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);
    var rowNum  = payload.historyRowNumber;

    if (!rowNum || typeof rowNum !== 'number') {
      return _jsonResponse({
        status: 'error',
        message: 'Missing or invalid historyRowNumber',
      });
    }

    var L = CONFIG.HISTORY_LAYOUT;
    Logger.log('[doPost] reading row %s from %s (width %s)',
               rowNum, CONFIG.HISTORY_SHEET_NAME, L.TOTAL_WIDTH);

    var ss    = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(CONFIG.HISTORY_SHEET_NAME);
    if (!sheet) {
      throw new Error('Sheet "' + CONFIG.HISTORY_SHEET_NAME + '" not found.');
    }

    var row = sheet.getRange(rowNum, 1, 1, L.TOTAL_WIDTH).getValues()[0];
    if (!row[L.COL_AGENT_NAME]) {
      return _jsonResponse({
        status: 'error',
        message: 'Analyst_History row ' + rowNum + ' appears empty',
      });
    }

    var entry = QAEntry.fromHistoryRow(row);
    _processHistoryRow(entry);

    return _jsonResponse({
      status: 'ok',
      message: 'Analyst_History row ' + rowNum + ' processed',
    });

  } catch (err) {
    Logger.log('[doPost ERROR] %s\n%s', err.message, err.stack);
    return _jsonResponse({ status: 'error', message: err.message });
  }
}

/**
 * Builds + sends the QA email for a QAEntry already populated by
 * `QAEntry.fromHistoryRow(row)` — Python's Stage 4 wrote scores,
 * reasoning, confidence, feedback, and caller meta to the row, and
 * resolved the agent's email via Mails.
 *
 * @param {QAEntry} entry
 * @private
 */
function _processHistoryRow(entry) {
  Logger.log('[_processHistoryRow] agent=%s, overallScore=%s, agentEmail=%s',
             entry.agentName, entry.overallScore, entry.agentEmail);

  var ss      = SpreadsheetApp.getActiveSpreadsheet();
  var history = new AnalystHistory(ss);

  // Stage 4 already wrote this call's row to Analyst_History, so
  // `getHistory` returns it as the most-recent entry. No prepend
  // needed — that would duplicate the current call in the progression
  // card. ProgressionCard marks `entries[length-1]` as current.
  var pastEntries = history.getHistory(entry.agentName);
  Logger.log('[_processHistoryRow] pastEntries count=%s', pastEntries.length);

  var scoreCard       = new ScoreCard(entry);
  var feedbackCard    = new FeedbackCard(entry);
  var progressionCard = new ProgressionCard(entry, pastEntries);
  var renderer        = new HtmlRenderer(entry, scoreCard, feedbackCard, progressionCard);

  var sender = new EmailSender(entry);
  sender.send(renderer.renderEmail());
  Logger.log('[_processHistoryRow] DONE — email sent to %s', entry.agentEmail);
}

function _jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
