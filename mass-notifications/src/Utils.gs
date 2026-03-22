/**
 * Mass Notifications — Utils
 * Pure utility functions: string helpers, HTML sanitizers, date utilities.
 * No direct GAS sheet/email API calls here — only Utilities.* and Session.*.
 */

// ── String / HTML helpers ─────────────────────────────────────────────────────

/**
 * Escapes all five HTML special characters.
 * Use when inserting arbitrary text into an HTML context.
 */
function escapeHtml_(s) {
  return String(s)
    .replaceAll('&',  '&amp;')
    .replaceAll('<',  '&lt;')
    .replaceAll('>',  '&gt;')
    .replaceAll('"',  '&quot;')
    .replaceAll("'",  '&#39;');
}

/**
 * Escapes &, <, > only — used for values inside tag attributes or plain text
 * blocks where quotes are not a concern.
 */
function sanitize_(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

/**
 * No-op pass-through for token values that are already trusted HTML.
 * Kept as a named function so we can tighten it later without touching callers.
 */
function sanitizeKeepBasicHtml_(s) {
  return String(s);
}

/**
 * Converts a boolean-ish value (YES/NO/TRUE/FALSE/1/0) to a JS boolean.
 * @param {*}       value
 * @param {boolean} defaultVal — returned when value is empty/unrecognised
 */
function toBool_(value, defaultVal) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number')  return value !== 0;
  const s = String(value == null ? '' : value).trim().toUpperCase();
  if (s === '') return !!defaultVal;
  if (['YES','Y','TRUE','T','ON','1'].includes(s))  return true;
  if (['NO','N','FALSE','F','OFF','0'].includes(s)) return false;
  return !!defaultVal;
}

/**
 * Derives a human-readable display name from an email local-part.
 * Examples:
 *   "maria@x.com"          → "Maria"
 *   "maximiliano.perez@x"  → "Maximiliano Perez"
 *   "april_kasky@x"        → "April Kasky"
 *   "jane+ops@x"           → "Jane"
 */
function deriveNameFromEmail_(email) {
  if (!email) return '';
  const raw   = String(email).trim();
  const at    = raw.indexOf('@');
  const local = (at > 0 ? raw.slice(0, at) : raw).replace(/\+.*$/, '');
  const words = local
    .replace(/[\._\-]+/g, ' ')
    .replace(/[^a-zA-ZÀ-ÖØ-öø-ÿ\s]/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean);

  if (!words.length) return '';
  return words.map(w =>
    w.length === 1 ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
  ).join(' ');
}

// ── Preview-safety helpers ────────────────────────────────────────────────────

/**
 * Normalizes tel: anchors for all HTML output (email sends and previews).
 * Fixes self-closing <a .../> and strips formatting characters from tel: URIs.
 */
function normalizeTelLinks_(html) {
  if (!html) return '';
  let out = String(html);
  out = out.replace(/<a([^>]*?)\/>/gi, '<a$1></a>');
  out = out.replace(/href\s*=\s*(['"])\s*tel\s*:\s*([^'"]+)\1/gi, (m, q, num) => {
    return `href="tel:${String(num).replace(/[^\d+]/g, '')}"`;
  });
  return out;
}

/**
 * Strips/normalises HTML that can trip up HtmlService modal rendering.
 * Does NOT affect real email sends — only used in dry-run previews.
 */
function makePreviewSafe_(html) {
  if (!html) return '';
  let out = String(html);
  out = out.replace(/<script[\s\S]*?<\/script>/gi, '');
  out = out.replace(/\sdir="[^"]*"/gi, '');
  out = out.replace(/<\/?tbody>/gi, '');
  return out || '';
}

// ── Spreadsheet helpers ───────────────────────────────────────────────────────

/**
 * Returns a unique sheet name by appending __1, __2, … if the base name
 * already exists in the active spreadsheet.
 */
function uniqueSheetName_(base) {
  const ss = SpreadsheetApp.getActive();
  let name = base, i = 1;
  while (ss.getSheetByName(name)) name = `${base}__${i++}`;
  return name;
}

/**
 * Deletes hidden archive sheets older than a given number of days.
 * Archive sheets are expected to follow the naming convention:
 *   Archive_<sheetName>_yyyyMMdd_HHmmss
 *
 * @param {number} [maxAgeDays=60] — sheets older than this are deleted
 * @return {number} The number of sheets deleted
 */
function purgeOldArchiveSheets(maxAgeDays) {
  if (maxAgeDays == null) maxAgeDays = 60;
  const ss      = SpreadsheetApp.getActive();
  const cutoff  = new Date();
  cutoff.setDate(cutoff.getDate() - maxAgeDays);

  const pattern = /^Archive_.+_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/;
  let deleted   = 0;

  ss.getSheets().forEach(sh => {
    if (!sh.isSheetHidden()) return;
    const m = sh.getName().match(pattern);
    if (!m) return;

    const sheetDate = new Date(
      parseInt(m[1]), parseInt(m[2]) - 1, parseInt(m[3]),
      parseInt(m[4]), parseInt(m[5]),      parseInt(m[6])
    );
    if (sheetDate < cutoff) {
      ss.deleteSheet(sh);
      deleted++;
    }
  });

  Logger.log(`Purged ${deleted} archive sheet(s) older than ${maxAgeDays} days.`);
  return deleted;
}

/**
 * Logs total, visible, and hidden sheet counts for the active spreadsheet.
 * Run from the Apps Script editor to diagnose "Service Spreadsheets failed" errors
 * caused by hitting Google's per-spreadsheet sheet/cell limits.
 */
function countSheets() {
  const ss     = SpreadsheetApp.getActive();
  const all    = ss.getSheets();
  const hidden = all.filter(s => s.isSheetHidden());
  Logger.log(`Total sheets: ${all.length}, Hidden: ${hidden.length}, Visible: ${all.length - hidden.length}`);
}

/**
 * Parses a comma/whitespace-separated string of Drive file IDs.
 * Deduplicates and strips empty tokens.
 */
function parseIdList_(raw) {
  if (!raw) return [];
  return String(raw)
    .split(/[,\s]+/)
    .map(s => s.trim())
    .filter(Boolean)
    .filter((v, i, a) => a.indexOf(v) === i);
}

// ── Date utilities ────────────────────────────────────────────────────────────

/**
 * Computes the notification window [start, end] from config values.
 * Falls back to today → tomorrow when values are missing.
 */
function computeWindow_(startVal, endVal, tz) {
  const todayMid = new Date(Utilities.formatDate(new Date(), tz, "yyyy-MM-dd'T'00:00:00"));
  const start    = coerceToDateAtMidnight_(startVal, tz) || todayMid;
  const end      = coerceToDateAtMidnight_(endVal,   tz) || new Date(start.getTime() + 86400000);
  return { start, end };
}

function coerceToDateAtMidnight_(val, tz) {
  if (!val) return null;
  if (Object.prototype.toString.call(val) === '[object Date]' && !isNaN(val)) {
    return new Date(Utilities.formatDate(val, tz, "yyyy-MM-dd'T'00:00:00"));
  }
  if (typeof val === 'string') return parseIsoDate_(val, tz);
  return null;
}

function parseIsoDate_(str, tz) {
  if (!str || typeof str !== 'string') return null;
  const m = str.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  const dt = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00`);
  if (isNaN(dt.getTime())) return null;
  const stamp  = Utilities.formatDate(dt, tz, "yyyy-MM-dd'T'HH:mm:ss");
  const tzDate = new Date(stamp);
  return isNaN(tzDate.getTime()) ? dt : tzDate;
}

/**
 * Formats a date range as a human-readable string, collapsing same-day or
 * same-year ranges intelligently.
 */
function formatDateRange_(startDate, endDate, tz) {
  const fmt = (d, withYear = false) =>
    Utilities.formatDate(d, tz, withYear ? 'EEE, MMM d, yyyy' : 'EEE, MMM d');

  const sameDay  = startDate.getFullYear() === endDate.getFullYear()
                && startDate.getMonth()    === endDate.getMonth()
                && startDate.getDate()     === endDate.getDate();
  if (sameDay) return fmt(startDate, true);

  const sameYear = startDate.getFullYear() === endDate.getFullYear();
  return sameYear
    ? `${fmt(startDate, false)}–${fmt(endDate, true)}`
    : `${fmt(startDate, true)}–${fmt(endDate, true)}`;
}
