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
 * Payload shapes (two modes):
 *
 *   Row mode (Railway / FastAPI — reads Analyst_History):
 *   { "historyRowNumber": <int>,
 *     "disclaimer": "<cause>"?    // ScorecardActions S7 — optional tag
 *                                 // (rescore_manual | rescore_auto |
 *                                 //  override | review_resolution |
 *                                 //  edit_finalized) rendered as a
 *                                 //  banner naming why this email
 *                                 //  supersedes a previous one
 *   }
 *
 *   Payload mode (Sandy — SandyMigration email slice): fully
 *   self-contained; NO sheet reads. Sandy-born evaluations never reach
 *   Analyst_History (the shadow sync is one-way into D1), so the app
 *   sends everything inline — including the progression history, which
 *   D1 covers more completely than the sheet once Sandy scoring is live.
 *   { "entry": {
 *       agentName, agentEmail, managerEmail, dialpadLink,
 *       "timestamp": "<ISO 8601>",          // call_connected_at
 *       overallScore: <number>,
 *       numericScores: { "<section_id>": <number|null NA> },
 *       binaryChecks:  { "<section_id>": <true|false|null NA> },
 *       aiReasoning:   { "<section_id>": "<text>" },
 *       aiConfidence:  { "<section_id>": "high|medium|low|''" },
 *       strengths, improvements, callSummary, callerName, callerPhone,
 *       disposition, aiCsat,
 *       sopReferences: ["SOP 1: Title", ...]
 *     },
 *     "pastEntries": [                       // newest-first, CURRENT
 *       { "timestamp": "<ISO>",              // call INCLUDED as [0]
 *         "overallScore": <number>,          // (getHistory contract)
 *         "source": "<ai|manual|ai_reviewed>" }, ...
 *     ],
 *     "disclaimer": "<cause>"?
 *   }
 *
 * Response:
 *   { "status": "ok"|"error", "message": "..." }
 */

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);
    var disclaimer = (typeof payload.disclaimer === 'string')
      ? payload.disclaimer : '';

    // ── Payload mode (Sandy) — self-contained, no sheet reads ──────
    if (payload.entry && typeof payload.entry === 'object') {
      var pEntry = QAEntry.fromPayload(payload.entry);
      var history = (payload.pastEntries || []).map(function(p) {
        return {
          timestamp:    new Date(p.timestamp),
          overallScore: parseFloat(p.overallScore) || 0,
          source:       (p.source || '').toString(),
        };
      });
      Logger.log('[doPost] payload mode: agent=%s, history=%s entries',
                 pEntry.agentName, history.length);
      _processEntry(pEntry, history, disclaimer);
      return _jsonResponse({
        status: 'ok',
        message: 'payload-mode email dispatched to ' + pEntry.agentEmail,
      });
    }

    // ── Row mode (Railway) — read Analyst_History ──────────────────
    var rowNum = payload.historyRowNumber;

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

    if (disclaimer) {
      Logger.log('[doPost] disclaimer cause: %s', disclaimer);
    }
    var entry = QAEntry.fromHistoryRow(row);
    _processHistoryRow(entry, disclaimer);

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
 * @param {string=} disclaimer — optional §4.2.7/§4.3a cause tag; rendered
 *                  as a banner above the cards (see HtmlRenderer).
 * @private
 */
function _processHistoryRow(entry, disclaimer) {
  var ss      = SpreadsheetApp.getActiveSpreadsheet();
  var history = new AnalystHistory(ss);

  // Stage 4 already wrote this call's row to Analyst_History, so
  // `getHistory` returns it as the most-recent entry. No prepend
  // needed — that would duplicate the current call in the progression
  // card. ProgressionCard marks `entries[length-1]` as current.
  var pastEntries = history.getHistory(entry.agentName);
  _processEntry(entry, pastEntries, disclaimer);
}

/**
 * Shared card-building + send core for both modes. `pastEntries` follows
 * the AnalystHistory.getHistory contract: newest-first, current call
 * included as the most-recent entry (payload mode senders must honor
 * this — Sandy queries D1 AFTER persisting the evaluation, so the
 * current call is naturally the newest row).
 *
 * @param {QAEntry}  entry
 * @param {Object[]} pastEntries
 * @param {string=}  disclaimer
 * @private
 */
function _processEntry(entry, pastEntries, disclaimer) {
  Logger.log('[_processEntry] agent=%s, overallScore=%s, agentEmail=%s, history=%s',
             entry.agentName, entry.overallScore, entry.agentEmail,
             pastEntries.length);

  var scoreCard       = new ScoreCard(entry);
  var feedbackCard    = new FeedbackCard(entry);
  var progressionCard = new ProgressionCard(entry, pastEntries);
  var renderer        = new HtmlRenderer(entry, scoreCard, feedbackCard, progressionCard, disclaimer);

  var sender = new EmailSender(entry);
  sender.send(renderer.renderEmail());
  Logger.log('[_processEntry] DONE — email sent to %s', entry.agentEmail);
}

function _jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
