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
  const cfg = loadConfig_();
  const tz  = cfg.timezone || 'America/Mexico_City';
  return {
    // Date objects (cfg.start / cfg.end) don't survive the google.script.run
    // JSON serialisation cleanly across all browsers. Format them as plain
    // yyyy-MM-dd strings server-side so <input type="date"> can consume them
    // directly without any client-side date-parsing gymnastics.
    config: Object.assign({}, cfg, {
      windowStartStr: cfg.start ? Utilities.formatDate(cfg.start, tz, 'yyyy-MM-dd') : '',
      windowEndStr:   cfg.end   ? Utilities.formatDate(cfg.end,   tz, 'yyyy-MM-dd') : '',
    }),
    recipients: getRecipientsForWebApp_(),
    templates:  Object.keys(EMAIL_TEMPLATES),
    cards:      Object.keys(CARD_REGISTRY),
  };
}

/**
 * Reads the recipients sheet and returns rows as plain objects.
 * Skips blank rows (no email).
 *
 * @return {Array<{email: string, name: string, unit: string}>}
 */
function getRecipientsForWebApp_() {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(DEFAULT_RECIPIENTS_SHEET);
  if (!sh || sh.getLastRow() < 2) return [];

  return sh
    .getRange(2, 1, sh.getLastRow() - 1, COL_MAX)
    .getValues()
    .filter(r => String(r[COL.EMAIL - 1]).trim())
    .map(r => ({
      email: r[COL.EMAIL - 1],
      name:  r[COL.NAME  - 1],
      unit:  r[COL.UNIT  - 1],
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
 * the hint text as a string (instead of calling safeAlert_ which pops a
 * dialog in the spreadsheet UI, not the web app).
 *
 * Called via google.script.run.loadTemplateFrontend(name).
 *
 * @param  {string}      templateName — must match a key in EMAIL_TEMPLATES
 * @return {string|null} hint text, or null if the template has no _hint
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

  return template._hint || null;
}

// ── Client → Server: preview ──────────────────────────────────────────────────

/**
 * Returns a rendered HTML string for a live email preview.
 *
 * The front-end calls saveRecipients() and saveConfigValue() first to persist
 * the current form state to the sheets, then calls this function. We read
 * everything from the sheets (same path as a real send) so the preview is
 * guaranteed to match what would actually go out.
 *
 * Returns the HTML for the FIRST eligible recipient only (row 2).
 *
 * @return {string} Rendered email HTML, or an error message string.
 */
function getEmailPreview() {
  try {
    const cfg  = loadConfig_();
    const html = buildPreviewHtml_(cfg);
    return html;
  } catch (e) {
    return `<p style="color:red;font-family:sans-serif;">Preview error: ${e.message}</p>`;
  }
}

/**
 * Builds a preview HTML string from the first eligible recipient row.
 * Stub — will be wired to the existing template/token pipeline in a later pass.
 *
 * @param {Object} cfg — result of loadConfig_()
 * @return {string}
 */
function buildPreviewHtml_(cfg) {
  // TODO: wire to buildEmailBody_() / renderWithTokens_() pipeline.
  // Returning a placeholder until WebApp_Preview.html is built.
  return `
    <p style="font-family:Arial,sans-serif;color:#555;padding:16px;">
      Preview will render here. Config loaded: <strong>${cfg.propertyName || '(no property name)'}</strong>
    </p>`;
}

// ── Client → Server: send ─────────────────────────────────────────────────────

/**
 * Sends the mass notification, then resets the sheet.
 *
 * The front-end must call saveRecipients() and saveConfigValue() for all
 * changed fields BEFORE calling this — we read everything from the sheets.
 *
 * @return {{ count: number, runLogRow: number }}
 *   count      — number of emails sent
 *   runLogRow  — Run_Log row the operator can use with restoreRecipientsFromRow()
 */
function webAppSend() {
  // TODO: call sendMassNotifications() once the send pipeline is wired to
  // accept a caller-supplied cfg rather than always calling loadConfig_() itself.
  // For now this is a guarded stub so the button exists but does nothing destructive.
  throw new Error('Send not yet wired — coming in the next development pass.');
}
