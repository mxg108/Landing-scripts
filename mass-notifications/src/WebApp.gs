/**
 * Mass Notifications — WebApp
 *
 * Entry point and server-side API for the Web App front-end.
 *
 * HOW A GAS WEB APP WORKS (primer):
 *   1. You deploy this script as a Web App via Apps Script → Deploy.
 *   2. GAS calls doGet(e) whenever someone opens the Web App URL in a browser.
 *   3. doGet() returns an HtmlOutput — the HTML page the browser renders.
 *   4. Inside that page, JavaScript calls back to THIS file's functions using
 *      the special google.script.run API (browser → server bridge).
 *
 * ARCHITECTURE DECISION — "thin UI, thick sheet":
 *   Rather than building a parallel send engine in the Web App, the front-end
 *   writes form data back to the Config and Mass_Notification sheets, then
 *   delegates to the existing server-side functions (loadConfig_, sendMass-
 *   Notifications, etc.). This keeps all business logic in one place and lets
 *   the spreadsheet UI and the Web App stay in sync with zero duplication.
 *
 * FILE LAYOUT:
 *   WebApp.gs              ← this file (doGet + server API)
 *   WebApp.html            ← HTML shell; loads the three section partials
 *   WebApp_Recipients.html ← Section 1: editable recipients grid
 *   WebApp_Config.html     ← Section 2: config fields + rich-text editor
 *   WebApp_Preview.html    ← Section 3: live email preview iframe
 */

// ── Entry point ───────────────────────────────────────────────────────────────

/**
 * Called by GAS when the Web App URL is opened in a browser.
 *
 * HtmlService.createTemplateFromFile() lets us use <?!= ... ?> tags inside
 * the HTML to run server-side GAS code at render time (like server-side
 * templating in other frameworks). evaluate() executes those tags and returns
 * a plain HtmlOutput the browser can render.
 *
 * @param {Object} e — request object (e.parameter for query params, etc.)
 * @return {HtmlOutput}
 */
function doGet(e) {
  return HtmlService
    .createTemplateFromFile('Index')
    .evaluate()
    .setTitle('Mass Notifications')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    // ALLOWALL lets the page load in iframes (e.g. Looker embeds later).
    // Change to SAMEORIGIN if you want to lock it down.
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

/**
 * GAS template include helper — the equivalent of an import/partial in
 * other frameworks.
 *
 * Usage inside any .html file:
 *   <?!= include('WebApp_Recipients') ?>
 *
 * This lets us split the UI across multiple HTML files while GAS stitches
 * them into one page at render time.
 *
 * @param {string} filename — name of an .html file in the project (no extension)
 * @return {string} raw HTML content of that file
 */
function include(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

// ── Server → Client: initial load ─────────────────────────────────────────────

/**
 * Returns everything the Web App needs to render on first load.
 * The browser calls this ONCE via google.script.run.getInitialData().
 *
 * Returning one big object in a single round-trip is important because every
 * google.script.run call has ~1–3 s of GAS cold-start overhead. Batching
 * avoids loading the page in multiple slow stages.
 *
 * @return {{
 *   config:     Object,   — current Config sheet values (from loadConfig_())
 *   recipients: Array,    — rows from Mass_Notification sheet
 *   templates:  string[], — available template names
 *   cards:      string[], — available notification card types
 * }}
 */
function getInitialData() {
  // Strip the non-serialisable Date objects from cfg before sending to the
  // client. Replace them with pre-formatted yyyy-MM-dd strings so that
  // <input type="date"> can consume them directly.
  const cfg = loadConfig_();
  const tz  = cfg.timezone || 'America/Mexico_City';
  const clientCfg = Object.assign({}, cfg);
  delete clientCfg.start;
  delete clientCfg.end;
  clientCfg.windowStartStr = cfg.start ? Utilities.formatDate(cfg.start, tz, 'yyyy-MM-dd') : '';
  clientCfg.windowEndStr   = cfg.end   ? Utilities.formatDate(cfg.end,   tz, 'yyyy-MM-dd') : '';

  const result = {
    config:     clientCfg,
    recipients: getRecipientsForWebApp_(cfg.recipientsSheetName),
    templates:  Object.keys(EMAIL_TEMPLATES),
    cards:      Object.keys(CARD_REGISTRY),
  };

  // Verify serializability before returning. GAS silently passes null to the
  // success handler when the return value contains a non-serialisable type
  // (e.g. a Date object that slipped through). Catching it here converts the
  // silent null into a real error that routes to the failure handler instead.
  try {
    JSON.stringify(result);
  } catch (e) {
    throw new Error('getInitialData: payload not JSON-serialisable — ' + e.message +
      '. clientCfg keys: ' + Object.keys(clientCfg).join(', '));
  }

  return result;
}

/**
 * Converts a Google Sheets cell value to a plain string safe for JSON.
 * Sheets formats numeric unit numbers (e.g. 1101) as Date objects when the
 * column has date formatting — in that case we extract the year, which is the
 * original numeric value Sheets mis-interpreted as a year.
 */
function _sheetCellToStr_(v) {
  if (v instanceof Date) return String(v.getFullYear());
  return String(v == null ? '' : v);
}

/**
 * Reads the recipients sheet and returns rows as plain objects.
 * Skips blank rows (no email).
 *
 * @param  {string=} sheetName — overrides DEFAULT_RECIPIENTS_SHEET when supplied
 * @return {Array<{email: string, name: string, unit: string}>}
 */
function getRecipientsForWebApp_(sheetName) {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(sheetName || DEFAULT_RECIPIENTS_SHEET);
  if (!sh || sh.getLastRow() < 2) return [];

  return sh
    .getRange(2, 1, sh.getLastRow() - 1, COL_MAX)
    .getValues()
    .filter(r => String(r[COL.EMAIL - 1]).trim())
    .map(r => ({
      email: String(r[COL.EMAIL - 1] || ''),
      name:  String(r[COL.NAME  - 1] || ''),
      unit:  _sheetCellToStr_(r[COL.UNIT - 1]),
    }));
}

// ── Client → Server: save form data ───────────────────────────────────────────

/**
 * Writes the recipients grid from the Web App to the Mass_Notification sheet.
 * Overwrites all existing data rows (keeps the header).
 *
 * Called via google.script.run.saveRecipients(rows) before preview or send.
 *
 * @param {Array<{email: string, name: string, unit: string}>} rows
 * @return {number} Number of rows written.
 */
function saveRecipients(rows) {
  if (!Array.isArray(rows) || !rows.length) return 0;

  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(DEFAULT_RECIPIENTS_SHEET);
  if (!sh) throw new Error(`Sheet not found: "${DEFAULT_RECIPIENTS_SHEET}"`);

  // Clear everything below the header row.
  if (sh.getLastRow() > 1) {
    sh.getRange(2, 1, sh.getLastRow() - 1, COL_MAX).clearContent();
  }

  // Write all rows in one batch — far fewer API calls than row-by-row.
  const data = rows.map(({ email, name, unit }) => {
    const out = new Array(COL_MAX).fill('');
    out[COL.EMAIL - 1] = email || '';
    out[COL.NAME  - 1] = name  || '';
    out[COL.UNIT  - 1] = unit  || '';
    return out;
  });

  sh.getRange(2, 1, data.length, COL_MAX).setValues(data);

  // Force the unit column to plain-text number format so Sheets does not
  // re-interpret numeric unit numbers (e.g. 1101) as dates on the next read.
  sh.getRange(2, COL.UNIT, data.length, 1).setNumberFormat('@');

  return data.length;
}

/**
 * Writes a single config key/value pair to the Config sheet.
 * Wraps the existing setConfigValue_() helper.
 *
 * Called via google.script.run.saveConfigValue(key, value).
 *
 * @param {string} key
 * @param {string|boolean} value
 */
function saveConfigValue(key, value) {
  setConfigValue_(key, value);
}

// ── Client → Server: templates ────────────────────────────────────────────────

/**
 * Web-App-safe version of loadTemplate_().
 * Writes the template's config values to the Config sheet, then returns
 * both the hint text and the refreshed config in a single call — so the
 * client can populate all fields without a second getInitialData() round-trip.
 *
 * Called via google.script.run.loadTemplateFrontend(name).
 *
 * @param  {string} templateName — must match a key in EMAIL_TEMPLATES
 * @return {{ hint: string|null, config: Object }}
 */
function loadTemplateFrontend(templateName) {
  const template = EMAIL_TEMPLATES[templateName];
  if (!template) throw new Error(`Template "${templateName}" not found.`);

  Object.entries(template).forEach(([key, value]) => {
    if (!key.startsWith('_')) setConfigValue_(key, value);
  });

  TEMPLATE_BLANK_KEYS.forEach(key => {
    if (!(key in template)) setConfigValue_(key, '');
  });

  // Read back the updated config so the client can populate all fields in
  // one round-trip instead of making a separate getInitialData() call.
  // Strip non-serialisable Date objects (same pattern as getInitialData).
  const cfg = loadConfig_();
  const tz  = cfg.timezone || 'America/Mexico_City';
  const clientCfg = Object.assign({}, cfg);
  delete clientCfg.start;
  delete clientCfg.end;
  clientCfg.windowStartStr = cfg.start ? Utilities.formatDate(cfg.start, tz, 'yyyy-MM-dd') : '';
  clientCfg.windowEndStr   = cfg.end   ? Utilities.formatDate(cfg.end,   tz, 'yyyy-MM-dd') : '';
  return {
    hint:   template._hint || null,
    config: clientCfg,
  };
}

// ── Client → Server: attachments ──────────────────────────────────────────────

/**
 * Resolves a comma-separated string of Drive file IDs to display names.
 * Used by the Web App Config section to let the operator verify IDs before send.
 *
 * Wraps summarizeAttachmentNames_() which does NOT download blobs — it only
 * fetches file metadata, so it's fast and safe to call from the UI.
 *
 * @param  {string} idsStr — comma-separated Drive file IDs
 * @return {{ names: string[], notes: string[] }}
 */
function resolveAttachmentNames(idsStr) {
  try {
    const ids = parseIdList_(idsStr || '');
    return summarizeAttachmentNames_(ids);
  } catch (e) {
    return { names: [], notes: ['Error: ' + e.message] };
  }
}

// ── Client → Server: preview ──────────────────────────────────────────────────

/**
 * Returns a rendered preview of the email for the first eligible recipient.
 *
 * The client saves recipients + config to the sheets first (same as before
 * a real send), then calls this. We read everything from the sheets so the
 * preview is guaranteed to match what would actually go out.
 *
 * @return {{ html: string, recipientEmail: string|null, error: string|null }}
 */
function getEmailPreview() {
  try {
    const cfg    = loadConfig_();
    const result = buildPreviewHtml_(cfg);
    return { html: result.html, recipientEmail: result.recipientEmail, error: null };
  } catch (e) {
    return { html: '', recipientEmail: null, error: e.message };
  }
}

/**
 * Builds a preview HTML string using the real send pipeline.
 * Uses the first eligible recipient row for per-row tokens; falls back to
 * generic tokens if the recipients sheet is empty or has no eligible rows.
 *
 * @param  {Object} cfg — result of loadConfig_()
 * @return {{ html: string, recipientEmail: string|null }}
 */
function buildPreviewHtml_(cfg) {
  const sh      = getRecipientsSheet_(cfg);
  const targets = getTargetRows_(sh, 1);

  let tokens;
  let recipientEmail = null;

  if (targets.length) {
    tokens         = buildPerRowTokens_(cfg, targets[0]);
    recipientEmail = targets[0].email;
  } else {
    // No eligible rows — use global tokens with placeholder values
    tokens = Object.assign(buildGlobalTokens_(cfg), {
      first_name:    'Resident',
      member_name:   'Resident',
      member_email:  '',
      unit:          '101',
    });
  }

  return {
    html:           buildHtmlBody_(cfg, tokens, tokens.unit || ''),
    recipientEmail: recipientEmail,
  };
}

// ── Client → Server: send ─────────────────────────────────────────────────────

/**
 * Sends the mass notification from the Web App.
 *
 * The client must call saveRecipients() BEFORE calling this so the sheet
 * reflects the current recipients grid.  Config fields are auto-saved on
 * blur, so they're already up to date.
 *
 * safeAlert_()    — no-op in Web App context (try-catch fallback to Logger)
 * confirmProceed_() — returns true automatically in headless/non-UI context
 * So calling sendModeIndividual_() / sendModeBcc_() directly is safe here.
 *
 * @param  {boolean} skipWarnings — pass true to send despite config warnings
 * @return {{
 *   ok:              boolean,
 *   count:           number,    sent count   (set when ok: true)
 *   runLogRow:       number,    Run_Log row  (set when ok: true)
 *   errors:          string[],  blocking errors
 *   warnings:        string[],  non-blocking warnings
 *   requiresConfirm: boolean,   true when only warnings exist (no errors)
 * }}
 */
function webAppSend(skipWarnings) {
  const cfg = loadConfig_();
  const v   = validateConfig_(cfg);

  if (v.errors.length) {
    return { ok: false, errors: v.errors, warnings: v.warnings || [], requiresConfirm: false };
  }

  if (v.warnings.length && !skipWarnings) {
    return { ok: false, errors: [], warnings: v.warnings, requiresConfirm: true };
  }

  // Run the send pipeline. Both helper functions are safe in non-UI context.
  String(cfg.sendMode).toUpperCase() === 'BCC'
    ? sendModeBcc_(cfg)
    : sendModeIndividual_(cfg);

  // logRunComplete_() is called inside the send functions and appends the
  // final row to Run_Log. Read it back so the client can display it.
  const log       = getRunLogSheet_();
  const runLogRow = log.getLastRow();
  const count     = Number(log.getRange(runLogRow, 5).getValue() || 0);

  return { ok: true, count, runLogRow, errors: [], warnings: [] };
}

/**
 * Archives + clears recipients and restores all config keys to safe defaults.
 * Wraps fullResetForNextUse() — safe from Web App because every internal
 * call uses safeAlert_() (which silently logs in non-UI context).
 * The client should reload the page after this returns.
 *
 * @return {{ ok: boolean }}
 */
function webAppReset() {
  try {
    fullResetForNextUse();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}
