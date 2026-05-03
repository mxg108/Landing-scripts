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
 *   I  RowStates      (JSON — array of {row, email, name, unit, prevStatus,
 *                                        prevLastSent, newStatus, newLastSent})
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
 * The optional `schema` parameter selects which sheet columns to capture for
 * the "before" snapshot. Move-In Flow rows have a different column layout
 * than the resident sheet, so callers pass a per-mode schema.
 *
 * @param {{ mode, cfg, shName, subject, rows, emails, schema? }} params
 * @return {{ runId, mode, shName, cfgSnapshot, rowBefore, rowAfter, schema }}
 */
function logRunStart_({ mode, cfg, shName, subject, rows, emails, schema }) {
  const ss    = SpreadsheetApp.getActive();
  const sh    = ss.getSheetByName(shName);
  const runId = `${mode}-${Utilities.formatDate(new Date(), cfg.timezone, 'yyyyMMdd_HHmmss')}`;

  // Default schema = original resident layout (INDIVIDUAL/BCC modes).
  const sch = schema || {
    statusCol:   COL.STATUS,
    lastSentCol: COL.LAST_SENT,
    fields: [
      { name: 'email', col: COL.EMAIL },
      { name: 'name',  col: COL.NAME  },
      { name: 'unit',  col: COL.UNIT  },
    ],
  };

  const before = rows.map(r => {
    const rec = {
      row:          r,
      prevStatus:   sh ? (sh.getRange(r, sch.statusCol  ).getValue() || null) : null,
      prevLastSent: sh ? (sh.getRange(r, sch.lastSentCol).getValue() || null) : null,
    };
    sch.fields.forEach(f => {
      rec[f.name] = sh ? (sh.getRange(r, f.col).getValue() || '') : '';
    });
    return rec;
  });

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
    statusCol:     sch.statusCol,
    lastSentCol:   sch.lastSentCol,
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

  return { runId, mode, shName, cfgSnapshot, rowBefore: before, rowAfter: [], schema: sch };
}

/**
 * Move-In Flow schema for logRunStart_ — captures property/member identifiers
 * instead of resident email/name/unit, and points status/lastSent at the
 * Move-In column indices.
 */
function moveInRunSchema_() {
  return {
    statusCol:   MOVEIN_COL.STATUS,
    lastSentCol: MOVEIN_COL.LAST_SENT,
    fields: [
      { name: 'reservation_id', col: MOVEIN_COL.RESERVATION_ID },
      { name: 'property_name',  col: MOVEIN_COL.PROPERTY_NAME  },
      { name: 'property_email', col: MOVEIN_COL.PROPERTY_EMAIL },
      { name: 'member_name',    col: MOVEIN_COL.MEMBER_NAME    },
      { name: 'apt_number',     col: MOVEIN_COL.APT_NUMBER     },
    ],
  };
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

  // Spread `b` so per-mode schemas pass through (resident email/name/unit
  // OR move-in reservation_id/property_email/etc.) without explicit mapping.
  const rowStates = run.rowBefore.map(b => {
    const after = run.rowAfter.find(a => a.row === b.row) || {};
    return {
      ...b,
      newStatus:    after.status   ?? null,
      newLastSent:  after.lastSent ?? null,
    };
  });

  log.getRange(lastRow, 5).setValue(count);
  log.getRange(lastRow, 9).setValue(JSON.stringify(rowStates));
  log.getRange(lastRow,11).setValue(true);
  if (errorMsg) log.getRange(lastRow, 10).setValue(`ERROR: ${errorMsg}`);
}

// ── Restore ───────────────────────────────────────────────────────────────────

/**
 * Restores recipients recorded in a Run_Log row back to the recipients sheet.
 *
 * For each entry in the RowStates JSON (column I of the given log row):
 *   - Writes email       → COL.EMAIL
 *   - Writes name        → COL.NAME
 *   - Writes unit        → COL.UNIT
 *   - Restores prevStatus   → COL.STATUS
 *   - Restores prevLastSent → COL.LAST_SENT
 *
 * @param {number} logRow   — 1-based row number in the Run_Log sheet.
 * @param {string} [shName] — Recipients sheet to write into.
 *                            Defaults to the value recorded in column D of that log row.
 * @return {number} Number of recipient rows restored.
 */
function restoreRecipientsFromRow(logRow, shName) {
  const log       = getRunLogSheet_();
  const sheetName = shName
                    || String(log.getRange(logRow, 4).getValue()).trim()
                    || DEFAULT_RECIPIENTS_SHEET;

  const raw = log.getRange(logRow, 9).getValue();
  if (!raw) return 0;

  let states;
  try {
    states = JSON.parse(raw);
  } catch (e) {
    throw new Error(`Run_Log row ${logRow}: malformed RowStates JSON — ${e.message}`);
  }
  if (!Array.isArray(states) || !states.length) return 0;

  const recSheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
  if (!recSheet) throw new Error(`Recipients sheet not found: "${sheetName}"`);

  states.forEach(({ row, email, name, unit, prevStatus, prevLastSent }) => {
    if (!row || row < 2) return;
    recSheet.getRange(row, COL.EMAIL    ).setValue(email        ?? '');
    recSheet.getRange(row, COL.NAME     ).setValue(name         ?? '');
    recSheet.getRange(row, COL.UNIT     ).setValue(unit         ?? '');
    recSheet.getRange(row, COL.STATUS   ).setValue(prevStatus   ?? '');
    recSheet.getRange(row, COL.LAST_SENT).setValue(prevLastSent ?? '');
  });

  return states.length;
}

/**
 * Restores the config snapshot recorded in a Run_Log row back to the Config sheet.
 *
 * Reads column H (ConfigSnapshot JSON) from the given log row and writes each
 * value back using setConfigValue_().
 *
 * Snapshot key → Config sheet key mapping:
 *   sendMode      → send_mode
 *   managerEmail  → manager_email
 *   propertyName  → property_name
 *   eventName     → event_name
 *   timezone      → timezone
 *   window[0]     → window_start
 *   window[1]     → window_end
 *
 * @param {number} logRow — 1-based row number in the Run_Log sheet.
 */
function restoreConfigFromRow(logRow) {
  const log = getRunLogSheet_();
  const raw = log.getRange(logRow, 8).getValue();

  if (!raw) {
    safeAlert_(`Run_Log row ${logRow}: ConfigSnapshot is empty — nothing to restore.`);
    return;
  }

  let snap;
  try {
    snap = JSON.parse(raw);
  } catch (e) {
    safeAlert_(`Run_Log row ${logRow}: malformed ConfigSnapshot JSON — ${e.message}`);
    return;
  }

  const keyMap = {
    sendMode:     'send_mode',
    managerEmail: 'manager_email',
    propertyName: 'property_name',
    eventName:    'event_name',
    timezone:     'timezone',
  };

  const restored = [];

  for (const [snapKey, cfgKey] of Object.entries(keyMap)) {
    if (snap[snapKey] !== undefined) {
      setConfigValue_(cfgKey, snap[snapKey]);
      restored.push(`${cfgKey} = ${snap[snapKey]}`);
    }
  }

  if (Array.isArray(snap.window) && snap.window.length >= 2) {
    setConfigValue_('window_start', snap.window[0]);
    setConfigValue_('window_end',   snap.window[1]);
    restored.push(`window_start = ${snap.window[0]}`);
    restored.push(`window_end = ${snap.window[1]}`);
  }

  safeAlert_(
    restored.length
      ? `Config restored from Run_Log row ${logRow}:\n\n${restored.join('\n')}`
      : `Run_Log row ${logRow}: ConfigSnapshot contained no recognised keys.`
  );
}

// ── Row appending ─────────────────────────────────────────────────────────────

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
