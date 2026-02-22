/**
 * Mass Notifications — RunLog
 *
 * Audit trail for every send/draft/undo operation.
 *
 * Run_Log sheet columns (1-based):
 *   A  Timestamp
 *   B  RunID
 *   C  Mode           (SEND_INDIVIDUAL | SEND_BCC | DRYRUN_DRAFTS | UNDO)
 *   D  RecipientsSheet
 *   E  Count          (emails sent / rows affected)
 *   F  Actor          (email of the user who triggered the run)
 *   G  Subject        (truncated to 180 chars)
 *   H  ConfigSnapshot (JSON — version, sendMode, propertyName, eventName, window)
 *   I  RowStates      (JSON — array of {row, email, prevStatus, prevLastSent,
 *                                        newStatus, newLastSent})
 *   J  Notes          (error message or freeform)
 *   K  Completed      (TRUE/FALSE)
 */

// ── Sheet access ──────────────────────────────────────────────────────────────

function getRunLogSheet_() {
  const ss = SpreadsheetApp.getActive();
  let sh   = ss.getSheetByName(RUN_LOG_SHEET);
  if (!sh) {
    sh = ss.insertSheet(RUN_LOG_SHEET);
    sh.appendRow([
      'Timestamp','RunID','Mode','RecipientsSheet','Count','Actor',
      'Subject','ConfigSnapshot','RowStates','Notes','Completed',
    ]);
    sh.getRange('A1:K1').setFontWeight('bold').setBackground('#f1f3f4');
    sh.setColumnWidths(1, 11, 180);
  }
  return sh;
}

// ── Run lifecycle ─────────────────────────────────────────────────────────────

/**
 * Opens a new log entry (Completed = FALSE) and captures the BEFORE state of
 * every affected row.  Returns a run object to be passed to logRunComplete_().
 *
 * @param {{ mode, cfg, shName, subject, rows, emails }} params
 * @return {{ runId, mode, shName, cfgSnapshot, rowBefore, rowAfter }}
 */
function logRunStart_({ mode, cfg, shName, subject, rows, emails }) {
  const ss    = SpreadsheetApp.getActive();
  const sh    = ss.getSheetByName(shName);
  const runId = `${mode}-${Utilities.formatDate(new Date(), cfg.timezone, 'yyyyMMdd_HHmmss')}`;

  const before = rows.map(r => ({
    row:          r,
    email:        sh ? (sh.getRange(r, COL.EMAIL).getValue()     || '') : '',
    prevStatus:   sh ? (sh.getRange(r, COL.STATUS).getValue()   || null) : null,
    prevLastSent: sh ? (sh.getRange(r, COL.LAST_SENT).getValue() || null) : null,
  }));

  const cfgSnapshot = JSON.stringify({
    version:       VERSION,
    sendMode:      cfg.sendMode,
    managerEmail:  cfg.managerEmail,
    propertyName:  cfg.propertyName,
    eventName:     cfg.eventName,
    window: [
      Utilities.formatDate(cfg.start, cfg.timezone, 'yyyy-MM-dd'),
      Utilities.formatDate(cfg.end,   cfg.timezone, 'yyyy-MM-dd'),
    ],
    timezone:      cfg.timezone,
  });

  appendRunLogRow_({
    timestamp:    new Date(),
    runId,
    mode,
    shName,
    count:        0,
    actor:        Session.getActiveUser().getEmail(),
    subject:      (subject || '').slice(0, 180),
    cfgSnapshot,
    rowStates:    before,
    notes:        '',
    completed:    false,
  });

  return { runId, mode, shName, cfgSnapshot, rowBefore: before, rowAfter: [] };
}

/**
 * Updates the last log row with post-run state, count, and Completed = TRUE.
 *
 * @param {{ runId, rowBefore, rowAfter }} run — returned by logRunStart_()
 * @param {number}      count    — emails actually sent / drafts created
 * @param {string|null} errorMsg — set on failure; null on success
 */
function logRunComplete_(run, count, errorMsg) {
  const log     = getRunLogSheet_();
  const lastRow = log.getLastRow();

  const rowStates = run.rowBefore.map(b => {
    const after = run.rowAfter.find(a => a.row === b.row) || {};
    return {
      row:          b.row,
      email:        b.email,
      prevStatus:   b.prevStatus   ?? null,
      prevLastSent: b.prevLastSent ?? null,
      newStatus:    after.status   ?? null,
      newLastSent:  after.lastSent ?? null,
    };
  });

  log.getRange(lastRow, 5).setValue(count);
  log.getRange(lastRow, 9).setValue(JSON.stringify(rowStates));
  log.getRange(lastRow,11).setValue(true);
  if (errorMsg) log.getRange(lastRow, 10).setValue(`ERROR: ${errorMsg}`);
}

/**
 * Appends a single row to Run_Log.  Used by logRunStart_() and undoLastRunFromLog().
 *
 * @param {{ timestamp, runId, mode, shName, count, actor, subject,
 *           cfgSnapshot, rowStates, notes, completed }} params
 */
function appendRunLogRow_({
  timestamp, runId, mode, shName, count,
  actor, subject, cfgSnapshot, rowStates, notes, completed,
}) {
  getRunLogSheet_().appendRow([
    timestamp   || new Date(),
    runId       || '',
    mode        || '',
    shName      || '',
    count       || 0,
    actor       || '',
    (subject    || '').slice(0, 180),
    cfgSnapshot || '',
    JSON.stringify(rowStates || []),
    notes       || '',
    completed   || false,
  ]);
}
