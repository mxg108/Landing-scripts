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

    // ── Digest mode (Sandy daily cron) — summary email, no entry ───
    if (payload.digest && typeof payload.digest === 'object') {
      var d = payload.digest;
      var digestTo = (CONFIG.EMAIL && CONFIG.EMAIL.TO_OVERRIDE) || d.recipient || '';
      if (!digestTo) {
        return _jsonResponse({
          status: 'error',
          message: 'digest: no recipient (set EMAIL.TO_OVERRIDE in Branding.js)',
        });
      }
      GmailApp.sendEmail(
        digestTo,
        'Sofia AI QA Daily Digest — ' + (d.date || ''),
        'Sofia AI QA digest for ' + (d.date || '') + ' — open in an HTML client.',
        { htmlBody: _renderDigestHtml(d), name: 'Landing QA System' }
      );
      Logger.log('[doPost] digest mode → To: %s (%s)', digestTo, d.date);
      return _jsonResponse({
        status: 'ok',
        message: 'digest dispatched to ' + digestTo,
      });
    }

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
      // Delivery may be overridden team-side (EmailSender._recipients) —
      // the receipt should name where the mail actually went.
      var deliveredTo = (CONFIG.EMAIL && CONFIG.EMAIL.TO_OVERRIDE) || pEntry.agentEmail;
      return _jsonResponse({
        status: 'ok',
        message: 'payload-mode email dispatched to ' + deliveredTo,
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

/**
 * Minimal branded HTML for the daily digest ({digest} payload mode).
 * Deliberately lean — the scorecards themselves carry the detail; this is
 * the "did anything happen yesterday" pulse with a jump into the console.
 */
function _renderDigestHtml(d) {
  var c = CONFIG.COLORS;
  function row(label, value) {
    return '<tr>' +
      '<td style="padding:8px 14px;color:' + c.TEXT_GRAY + ';font-size:14px;">' + label + '</td>' +
      '<td style="padding:8px 14px;font-size:15px;font-weight:600;color:' + c.DARK_NAVY + ';text-align:right;">' + value + '</td>' +
      '</tr>';
  }
  var avg = (d.avg_approved === null || d.avg_approved === undefined) ? '—' : d.avg_approved;
  return '' +
    '<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;">' +
      '<div style="background:' + c.DARK_NAVY + ';color:#fff;padding:18px 22px;border-radius:8px 8px 0 0;">' +
        '<div style="font-size:18px;font-weight:700;">Sofia AI — QA Daily Digest</div>' +
        '<div style="font-size:12px;opacity:.75;margin-top:2px;">' + (d.date || '') + ' · last 24 hours</div>' +
      '</div>' +
      '<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f4;border-top:0;">' +
        row('Calls scored', d.scored_24h) +
        row('Reviews approved', d.approved_24h) +
        row('Avg approved score', avg) +
        row('Awaiting human review (total)', d.backlog_pending) +
        (d.queue_errors_24h
          ? row('<span style="color:' + c.RED + ';">Pipeline errors</span>',
                '<span style="color:' + c.RED + ';">' + d.queue_errors_24h + '</span>')
          : '') +
      '</table>' +
      '<div style="background:' + c.LIGHT_BLUE + ';padding:14px 22px;border-radius:0 0 8px 8px;border:1px solid #e2e8f4;border-top:0;">' +
        '<a href="' + (d.console_url || '#') + '" style="color:' + c.ACCENT_BLUE + ';font-size:14px;text-decoration:none;font-weight:600;">' +
          'Open the review console &rsaquo;</a>' +
      '</div>' +
    '</div>';
}

function _jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
