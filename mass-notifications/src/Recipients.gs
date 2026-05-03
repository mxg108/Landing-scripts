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

    if (!email || !email.includes('@')) continue;
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
    if (!email || !email.includes('@')) return;
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
    if (!email || !email.includes('@')) return;
    if (status && status !== 'PENDING' && status !== 'READY') return;
    const key = email.toLowerCase();
    if (!seen.has(key)) { seen.add(key); emails.push(email); }
  });

  return { emails, managerEmail: cfg.managerEmail };
}

// ── Move-In Flow mode ─────────────────────────────────────────────────────────

/**
 * Returns the Move-In Flow sheet, throwing a descriptive error if it's missing.
 * Honors cfg.recipientsSheetName when set (so operators can point Move-In mode
 * at a renamed tab), otherwise falls back to MOVE_IN_SHEET.
 */
function getMoveInSheet_(cfg) {
  const ss   = SpreadsheetApp.getActive();
  const name = String(cfg && cfg.recipientsSheetName || MOVE_IN_SHEET).trim();
  const sh   = ss.getSheetByName(name);
  if (!sh) {
    const available = ss.getSheets().map(s => s.getName()).join(', ');
    throw new Error(`Missing Move-In sheet: "${name}". Available: ${available}`);
  }
  return sh;
}

/**
 * Parses the Occupants cell, formatted as pipe-delimited records separated
 * by ';' or newline. Tolerates trailing separators and missing fields.
 *
 *   "Deborah Colledge|805-883-8485|dlcolledge@yahoo.com; Jane Doe|||"
 *
 * @param {*} raw
 * @return {Array<{name:string, phone:string, email:string}>}
 */
function parseOccupants_(raw) {
  if (raw == null) return [];
  return String(raw)
    .split(/[;\n]+/)
    .map(chunk => chunk.trim())
    .filter(Boolean)
    .map(chunk => {
      const parts = chunk.split('|').map(p => p.trim());
      return {
        name:  parts[0] || '',
        phone: parts[1] || '',
        email: parts[2] || '',
      };
    })
    .filter(o => o.name || o.phone || o.email);
}

/**
 * Parses the Vehicle Info cell, formatted as pipe-delimited records separated
 * by ';' or newline. Field order matches the operator-facing template hint:
 *
 *   "Year|Make|Model|Color|License Plate|State"
 *
 *   "2018|Toyota|4Runner|Black|LCS3767|TX; 2021|Honda|Civic|White||"
 *
 * Returns an empty array when no '|' is present anywhere — the caller falls
 * back to rendering the raw cell value, preserving backward compatibility
 * with rows that were entered as free-text before this format existed.
 *
 * @param {*} raw
 * @return {Array<{year:string, make:string, model:string, color:string,
 *                 plate:string, state:string}>}
 */
function parseVehicles_(raw) {
  if (raw == null) return [];
  const text = String(raw);
  if (!text.trim() || text.indexOf('|') === -1) return [];
  return text
    .split(/[;\n]+/)
    .map(chunk => chunk.trim())
    .filter(Boolean)
    .map(chunk => {
      const parts = chunk.split('|').map(p => p.trim());
      return {
        year:  parts[0] || '',
        make:  parts[1] || '',
        model: parts[2] || '',
        color: parts[3] || '',
        plate: parts[4] || '',
        state: parts[5] || '',
      };
    })
    .filter(v => v.year || v.make || v.model || v.color || v.plate || v.state);
}

/**
 * Parses the Pet/ESA Info cell, formatted as pipe-delimited records separated
 * by ';' or newline. Field order matches the operator-facing template hint:
 *
 *   "Animal|Breed|Weight|ESA?|Name"
 *
 *   "Dog|Golden Retriever|65 lbs|Yes|Buddy; Cat|Tabby|10 lbs|No|Whiskers"
 *
 * Returns [] when no '|' is present (caller falls back to raw rendering),
 * matching parseVehicles_ semantics for backward compatibility.
 *
 * @param {*} raw
 * @return {Array<{animal:string, breed:string, weight:string,
 *                 esa:string, name:string}>}
 */
function parsePets_(raw) {
  if (raw == null) return [];
  const text = String(raw);
  if (!text.trim() || text.indexOf('|') === -1) return [];
  return text
    .split(/[;\n]+/)
    .map(chunk => chunk.trim())
    .filter(Boolean)
    .map(chunk => {
      const parts = chunk.split('|').map(p => p.trim());
      return {
        animal: parts[0] || '',
        breed:  parts[1] || '',
        weight: parts[2] || '',
        esa:    parts[3] || '',
        name:   parts[4] || '',
      };
    })
    .filter(p => p.animal || p.breed || p.weight || p.esa || p.name);
}

/**
 * Parses the Property Email cell into an array of valid email addresses.
 * Accepts comma, semicolon, whitespace, or newline as separators.
 */
function parsePropertyEmails_(raw) {
  if (!raw) return [];
  return String(raw)
    .split(/[,;\s]+/)
    .map(s => s.trim())
    .filter(s => s && s.includes('@'));
}

/**
 * Returns up to `limit` eligible Move-In rows.
 * A row is eligible when:
 *   • PROPERTY_EMAIL contains at least one address with '@'
 *   • STATUS is blank, PENDING, or READY
 *
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sh
 * @param {number} limit
 * @return {Array<Object>} — see field list in plan §2 (Recipients.gs).
 */
function getMoveInTargetRows_(sh, limit) {
  const lastRow = sh.getLastRow();
  if (lastRow < 2) return [];

  const data = sh.getRange(2, 1, lastRow - 1, MOVEIN_COL_MAX).getValues();
  const out  = [];

  for (let i = 0; i < data.length; i++) {
    const r = data[i];
    const propertyEmails = parsePropertyEmails_(r[MOVEIN_COL.PROPERTY_EMAIL - 1]);
    const status = String(r[MOVEIN_COL.STATUS - 1] || '').trim().toUpperCase();

    if (!propertyEmails.length) continue;
    if (status && status !== 'PENDING' && status !== 'READY') continue;

    out.push({
      row:            i + 2,
      reservationId:  String(r[MOVEIN_COL.RESERVATION_ID - 1] || '').trim(),
      propertyName:   String(r[MOVEIN_COL.PROPERTY_NAME   - 1] || '').trim(),
      propertyEmails,
      aptNumber:      String(r[MOVEIN_COL.APT_NUMBER      - 1] || '').trim(),
      memberName:     String(r[MOVEIN_COL.MEMBER_NAME     - 1] || '').trim(),
      memberEmail:    String(r[MOVEIN_COL.MEMBER_EMAIL    - 1] || '').trim(),
      memberPhone:    String(r[MOVEIN_COL.MEMBER_PHONE    - 1] || '').trim(),
      moveInDate:     r[MOVEIN_COL.MOVE_IN_DATE  - 1],   // raw — formatter handles Date or string
      moveOutDate:    r[MOVEIN_COL.MOVE_OUT_DATE - 1],
      vehicleInfo:    String(r[MOVEIN_COL.VEHICLE_INFO  - 1] || '').trim(),
      vehicles:       parseVehicles_(r[MOVEIN_COL.VEHICLE_INFO - 1]),
      petInfo:        String(r[MOVEIN_COL.PET_INFO      - 1] || '').trim(),
      pets:           parsePets_(r[MOVEIN_COL.PET_INFO - 1]),
      occupants:      parseOccupants_(r[MOVEIN_COL.OCCUPANTS - 1]),
      areaMgrName:    String(r[MOVEIN_COL.AREA_MGR_NAME  - 1] || '').trim(),
      areaMgrPhone:   String(r[MOVEIN_COL.AREA_MGR_PHONE - 1] || '').trim(),
      areaMgrEmail:   String(r[MOVEIN_COL.AREA_MGR_EMAIL - 1] || '').trim(),
      attachIds:      String(r[MOVEIN_COL.ATTACH_IDS     - 1] || '').trim(),
    });

    if (out.length >= limit) break;
  }

  return out;
}

/**
 * Counts eligible Move-In rows for the menu preview.
 * @return {{ count: number, properties: string[] }}
 */
function collectMoveInForCount_() {
  const cfg = loadConfig_();
  const sh  = getMoveInSheet_(cfg);
  const last = sh.getLastRow();
  if (last < 2) return { count: 0, properties: [] };

  const data    = sh.getRange(2, 1, last - 1, MOVEIN_COL_MAX).getValues();
  const props   = new Set();
  let   count   = 0;

  data.forEach(r => {
    const emails = parsePropertyEmails_(r[MOVEIN_COL.PROPERTY_EMAIL - 1]);
    const status = String(r[MOVEIN_COL.STATUS - 1] || '').trim().toUpperCase();
    if (!emails.length) return;
    if (status && status !== 'PENDING' && status !== 'READY') return;
    count++;
    const name = String(r[MOVEIN_COL.PROPERTY_NAME - 1] || '').trim();
    if (name) props.add(name);
  });

  return { count, properties: Array.from(props) };
}
