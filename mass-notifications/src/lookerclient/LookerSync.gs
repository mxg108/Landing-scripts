/**
 * Mass Notifications — Looker Sync
 *
 * Orchestrates the full pipeline:
 *   1. Prompt operator for property name
 *   2. Fetch active occupants from Looker API
 *   3. Write raw rows to "Looker_Data" sheet
 *   4. Sanitise & deduplicate (resolve shared emails via alt-email columns)
 *   5. Archive & silently clear Mass_Notification
 *   6. Populate Mass_Notification with sanitised rows
 *
 * Entry point: syncFromLooker()  (wired to the menu in UI.gs)
 */

// ── Entry point ───────────────────────────────────────────────────────────────

function syncFromLooker() {
  const ui  = SpreadsheetApp.getUi();
  const res = ui.prompt(
    'Fetch from Looker',
    'Enter the exact Property Name as it appears in Looker:',
    ui.ButtonSet.OK_CANCEL
  );
  if (res.getSelectedButton() !== ui.Button.OK) return;

  const propertyName = res.getResponseText().trim();
  if (!propertyName) return safeAlert_('Property name cannot be blank.');

  try {
    // 1. Fetch
    const raw = fetchActiveOccupants_(propertyName);
    if (!raw.length) {
      return safeAlert_(`No active occupants found for "${propertyName}".\nCheck the property name spelling.`);
    }

    // 2. Write raw data to Looker_Data sheet
    writeLookerDataSheet_(raw);

    // 3. Sanitise
    const sanitized = sanitizeOccupants_(raw);

    // 4. Archive & clear Mass_Notification (no intermediate alert)
    archiveAndClearMassNotificationQuiet_();

    // 5. Populate Mass_Notification
    const { pending, review } = populateRecipientsSheet_(sanitized);

    safeAlert_(
      `Looker sync complete for "${propertyName}".\n\n` +
      `Raw rows fetched : ${raw.length}\n` +
      `PENDING          : ${pending}  ← ready to send (Status = "PENDING")\n` +
      `REVIEW           : ${review}  ← duplicates that need manual resolution (Status = "REVIEW")\n\n` +
      (review > 0
        ? 'Some recipients share an email address with no available alternative.\n' +
          'Edit their Status or email in Mass_Notification before sending.'
        : 'All recipients resolved — ready to send.')
    );
  } catch (e) {
    safeAlert_(`Looker sync failed:\n${e.message}`);
  }
}

// ── Archive & clear (Looker-specific, mode-agnostic) ─────────────────────────

/**
 * Archives + clears the Mass_Notification sheet, regardless of cfg.sendMode.
 *
 * Looker sync ALWAYS targets Mass_Notification, so it must not go through
 * archiveAndClearRecipientsQuiet_ (mode-aware — clears Move_In_Flow when
 * sendMode === 'MOVE_IN') or getRecipientsSheet_ (honors
 * cfg.recipientsSheetName, which the MOVE_IN validator nudges operators to
 * point at Move_In_Flow). Either path would route Looker writes to the
 * wrong tab while MOVE_IN mode is active.
 */
function archiveAndClearMassNotificationQuiet_() {
  const cfg  = loadConfig_();
  const ss   = SpreadsheetApp.getActive();
  const sh   = ss.getSheetByName(DEFAULT_RECIPIENTS_SHEET);
  if (!sh) throw new Error(`Missing sheet: "${DEFAULT_RECIPIENTS_SHEET}".`);

  const last = sh.getLastRow();
  if (last < 2) return;

  const data = sh.getRange(2, 1, last - 1, COL_MAX).getValues();
  try {
    archiveCampaignToDb_(cfg, sh.getName(), data);
  } catch (e) {
    // DB write failure must not block the sync; Run_Log RowStates is the fallback.
    Logger.log('DB archive failed (data preserved in Run_Log): ' + e.message);
  }
  sh.getRange(2, 1, last - 1, COL_MAX).clearContent();
}

// ── Raw sheet writer ──────────────────────────────────────────────────────────

/**
 * Clears and repopulates the Looker_Data sheet with all raw rows
 * returned from the API. Creates the sheet if it doesn't exist.
 *
 * @param {Object[]} rows
 */
function writeLookerDataSheet_(rows) {
  const ss = SpreadsheetApp.getActive();
  let sh   = ss.getSheetByName(LOOKER_DATA_SHEET);
  if (!sh) {
    sh = ss.insertSheet(LOOKER_DATA_SHEET);
  } else {
    sh.clearContents();
  }

  const headers = [
    'Email Address', 'Member Name', 'Unit Number', 'Occupant Name',
    'Phone', 'Reservation ID', 'Check In', 'Check Out', 'Property Name',
    'Alt Email 1', 'Alt Email 2', 'Alt Email 3', 'Alt Email 4',
  ];
  const fields = [
    LFIELD.EMAIL,       LFIELD.MEMBER_NAME, LFIELD.UNIT,        LFIELD.OCC_NAME,
    LFIELD.PHONE,       LFIELD.RES_ID,      LFIELD.CHECK_IN,    LFIELD.CHECK_OUT,
    LFIELD.PROPERTY,    LFIELD.ALT_EMAIL_1, LFIELD.ALT_EMAIL_2, LFIELD.ALT_EMAIL_3,
    LFIELD.ALT_EMAIL_4,
  ];

  sh.getRange(1, 1, 1, headers.length)
    .setValues([headers])
    .setFontWeight('bold')
    .setBackground('#f1f3f4');

  if (rows.length) {
    const data = rows.map(r => fields.map(f => r[f] ?? ''));
    sh.getRange(2, 1, data.length, fields.length).setValues(data);
  }
}

// ── Deduplication & sanitisation ─────────────────────────────────────────────

/**
 * Groups rows by normalised primary email (column A) and resolves duplicates
 * using the alt-email columns (J–M).
 *
 * Rules:
 *   Group size = 1  → PENDING, email = A, name = B, unit = C
 *   Group size = 2  → Row 1 keeps primary email → PENDING
 *                     Row 2: first unused alt email found → PENDING
 *                            no alt email found           → collapsed (skip row 2)
 *   Group size ≥ 3  → All output rows → REVIEW
 *                     Rows that have an alt email get it assigned;
 *                     rows with no resolvable unique email stay on
 *                     the primary email so the operator can see the collision.
 *
 * @param {Object[]} rows — Raw Looker rows
 * @return {Array<{email, name, unit, status, notes}>}
 */
function sanitizeOccupants_(rows) {
  // Pre-filter: skip rows with no valid primary email
  const validRows = rows.filter(r => String(r[LFIELD.EMAIL] || '').trim().includes('@'));

  // Group by normalised primary email
  const groups = new Map();
  for (const r of validRows) {
    const key = String(r[LFIELD.EMAIL]).trim().toLowerCase();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }

  const result    = [];
  const usedEmails = new Set(); // normalised emails already committed to result

  for (const [primaryKey, group] of groups) {
    const isTriplicate = group.length >= 3;

    // Build alt-email pool: lowercase → original-cased value
    // Excludes the group's own primary email.
    const altMap = new Map();
    for (const r of group) {
      for (const f of [LFIELD.ALT_EMAIL_1, LFIELD.ALT_EMAIL_2, LFIELD.ALT_EMAIL_3, LFIELD.ALT_EMAIL_4]) {
        const raw  = String(r[f] || '').trim();
        const norm = raw.toLowerCase();
        if (raw.includes('@') && norm !== primaryKey && !altMap.has(norm)) {
          altMap.set(norm, raw);
        }
      }
    }

    // Row 1 always keeps the primary email
    const first = group[0];
    result.push({
      email:  String(first[LFIELD.EMAIL]).trim(),
      name:   String(first[LFIELD.MEMBER_NAME] || '').trim(),
      unit:   String(first[LFIELD.UNIT]        || '').trim(),
      status: isTriplicate ? 'REVIEW' : '',
      notes:  isTriplicate ? 'Triplicate+ group — manual review required' : '',
    });
    usedEmails.add(primaryKey);

    // Remaining rows in the group
    for (let i = 1; i < group.length; i++) {
      const r = group[i];

      // Find the first alt email not yet used anywhere in the output
      let assigned = null;
      for (const [norm, original] of altMap) {
        if (!usedEmails.has(norm)) {
          assigned = { norm, original };
          break;
        }
      }

      if (group.length === 2 && !assigned) {
        // Simple duplicate with no alt → collapse into row 1, skip this row
        continue;
      }

      if (assigned) {
        usedEmails.add(assigned.norm);
        altMap.delete(assigned.norm);
        result.push({
          email:  assigned.original,
          name:   String(r[LFIELD.MEMBER_NAME] || '').trim(),
          unit:   String(r[LFIELD.UNIT]        || '').trim(),
          status: isTriplicate ? 'REVIEW' : '',
          notes:  isTriplicate ? 'Triplicate+ group — manual review required' : '',
        });
      } else {
        // 3+ group, no alt available — write with primary email so the
        // collision is visible; operator must resolve before sending
        result.push({
          email:  String(first[LFIELD.EMAIL]).trim(),
          name:   String(r[LFIELD.MEMBER_NAME] || '').trim(),
          unit:   String(r[LFIELD.UNIT]        || '').trim(),
          status: 'REVIEW',
          notes:  'Triplicate+ group — no alt email available',
        });
      }
    }
  }

  return result;
}

// ── Sheet population ──────────────────────────────────────────────────────────

/**
 * Writes sanitised rows to Mass_Notification starting at row 2.
 * The sheet must already be cleared before calling this
 * (archiveAndClearMassNotificationQuiet_ handles that).
 *
 * Always targets DEFAULT_RECIPIENTS_SHEET directly — Looker is mode-agnostic
 * and must not honor cfg.recipientsSheetName, which would route writes to
 * Move_In_Flow when MOVE_IN mode is active.
 *
 * @param {Array<{email, name, unit, status, notes}>} rows
 * @return {{ pending: number, review: number }}
 */
function populateRecipientsSheet_(rows) {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(DEFAULT_RECIPIENTS_SHEET);
  if (!sh) throw new Error(`Missing sheet: "${DEFAULT_RECIPIENTS_SHEET}".`);
  let pending = 0, review = 0;

  const data = rows.map(({ email, name, unit, status, notes }) => {
    const resolvedStatus = status || 'PENDING';
    if (resolvedStatus === 'REVIEW') review++; else pending++;
    // Columns: EMAIL, NAME, UNIT, STATUS, LAST_SENT (blank), NOTES, ATTACH_IDS (blank)
    return [email, name, unit, resolvedStatus, '', notes, ''];
  });

  if (data.length) {
    sh.getRange(2, 1, data.length, COL_MAX).setValues(data);
  }

  return { pending, review };
}
