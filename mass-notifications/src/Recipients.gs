/**
 * Mass Notifications — Recipients
 * Sheet access and row selection for the recipients list.
 */

/**
 * Returns the recipients sheet, throwing a descriptive error if it's missing.
 * @param {Object} cfg — result of loadConfig_()
 * @return {GoogleAppsScript.Spreadsheet.Sheet}
 */
function getRecipientsSheet_(cfg) {
  const ss   = SpreadsheetApp.getActive();
  const name = String(cfg.recipientsSheetName || DEFAULT_RECIPIENTS_SHEET).trim();
  const sh   = ss.getSheetByName(name);
  if (!sh) {
    const available = ss.getSheets().map(s => s.getName()).join(', ');
    throw new Error(`Missing recipients sheet: "${name}". Available: ${available}`);
  }
  return sh;
}

/**
 * Returns up to `limit` eligible rows from the recipients sheet.
 * A row is eligible when:
 *   • Column A (Email) is non-empty
 *   • Column D (Status) is blank, PENDING, or READY
 *
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sh
 * @param {number} limit
 * @return {Array<{row: number, email: string, name: string, unit: string}>}
 */
function getTargetRows_(sh, limit) {
  const lastRow = sh.getLastRow();
  if (lastRow < 2) return [];

  const data = sh.getRange(2, 1, lastRow - 1, COL_MAX).getValues();
  const out  = [];

  for (let i = 0; i < data.length; i++) {
    const r      = data[i];
    const email  = String(r[COL.EMAIL  - 1] || '').trim();
    const name   = String(r[COL.NAME   - 1] || '').trim();
    const unit   = String(r[COL.UNIT   - 1] || '').trim();
    const status = String(r[COL.STATUS - 1] || '').trim().toUpperCase();

    if (!email) continue;
    if (status && status !== 'PENDING' && status !== 'READY') continue;

    out.push({ row: i + 2, email, name, unit });
    if (out.length >= limit) break;
  }

  return out;
}

/**
 * Collects all eligible email addresses and their row numbers for BCC sends.
 * Deduplicates by lower-cased address.
 *
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sh
 * @return {{ emails: string[], rows: number[] }}
 */
function collectEmailsForBcc_(sh) {
  const lastRow = sh.getLastRow();
  if (lastRow < 2) return { emails: [], rows: [] };

  const data   = sh.getRange(2, 1, lastRow - 1, COL_MAX).getValues();
  const emails = [];
  const rows   = [];
  const seen   = new Set();

  data.forEach((r, idx) => {
    const email  = String(r[COL.EMAIL  - 1] || '').trim();
    const status = String(r[COL.STATUS - 1] || '').trim().toUpperCase();
    if (!email) return;
    if (status && status !== 'PENDING' && status !== 'READY') return;

    const key = email.toLowerCase();
    if (!seen.has(key)) { seen.add(key); emails.push(email); }
    rows.push(idx + 2);
  });

  return { emails, rows };
}

/**
 * Counts eligible recipients — used by the "Preview recipients" menu item.
 * @return {{ emails: string[], managerEmail: string }}
 */
function collectEmailsForCount_() {
  const cfg     = loadConfig_();
  const sh      = getRecipientsSheet_(cfg);
  const lastRow = sh.getLastRow();
  if (lastRow < 2) return { emails: [], managerEmail: cfg.managerEmail };

  const data   = sh.getRange(2, 1, lastRow - 1, COL_MAX).getValues();
  const emails = [];
  const seen   = new Set();

  data.forEach(r => {
    const email  = String(r[COL.EMAIL  - 1] || '').trim();
    const status = String(r[COL.STATUS - 1] || '').trim().toUpperCase();
    if (!email) return;
    if (status && status !== 'PENDING' && status !== 'READY') return;
    const key = email.toLowerCase();
    if (!seen.has(key)) { seen.add(key); emails.push(email); }
  });

  return { emails, managerEmail: cfg.managerEmail };
}
