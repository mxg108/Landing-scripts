/**
 * Mass Notifications — UI
 * Menu construction, modal display helpers, and the About dialog.
 * This is the only file that calls SpreadsheetApp.getUi() directly.
 */

// ── Menu ──────────────────────────────────────────────────────────────────────

function onOpen() {
  const ui   = SpreadsheetApp.getUi();
  const main = ui.createMenu('Mass Notify');

  main.addItem('Preview recipients (count only)', 'previewRecipients');
  main.addSeparator();

  // ── Load Template submenu ────────────────────────────────────────────────
  const tmpl = ui.createMenu('Load Template');
  Object.keys(EMAIL_TEMPLATES).forEach(name => {
    // GAS menu items require a named global function, so we generate one per
    // template and register it on the global object.
    const fnName = `_loadTemplate_${name.replace(/\W+/g, '_')}`;
    if (typeof globalThis[fnName] !== 'function') {
      globalThis[fnName] = () => loadTemplate_(name);
    }
    tmpl.addItem(name, fnName);
  });
  main.addSubMenu(tmpl);
  main.addSeparator();

  // ── Dry Run submenu ──────────────────────────────────────────────────────
  const dry = ui.createMenu('Dry Run');
  dry.addItem('Preview first N (Individual, HTML)',    'dryRunPreview');
  dry.addItem('Create drafts (Individual, first N)',   'dryRunCreateDrafts');
  dry.addItem('Test send to me (Individual)',          'dryRunTestSendToMe');
  dry.addSeparator();

  const dryBcc = ui.createMenu('BCC mode');
  dryBcc.addItem('Preview BCC (HTML)',    'dryRunPreviewBCC');
  dryBcc.addItem('Create BCC draft',      'dryRunCreateDraftsBCC');
  dryBcc.addItem('Test send to me',       'dryRunTestSendToMeBCC');
  dry.addSubMenu(dryBcc);
  main.addSubMenu(dry);
  main.addSeparator();

  // ── Send ─────────────────────────────────────────────────────────────────
  main.addItem('Send', 'sendMassNotifications');
  main.addSeparator();

  // ── Reset / Archive ──────────────────────────────────────────────────────
  const reset = ui.createMenu('Reset / Archive');
  reset.addItem('Archive recipients',               'archiveAndClearRecipients');
  reset.addItem('Reset statuses & timestamps',      'resetStatusesOnly');
  reset.addItem('Full reset',                       'fullResetForNextUse');
  reset.addItem('Undo LAST run (from Run_Log)',      'undoLastRunFromLog');
  main.addSubMenu(reset);
  main.addSeparator();

  // ── Signature ────────────────────────────────────────────────────────────
  const sig = ui.createMenu('Signature');
  sig.addItem('Sync from Gmail (primary alias)', 'syncSignatureFromGmailToConfig');
  sig.addItem('Clear signature_html',             'clearSignatureInConfig');
  main.addSubMenu(sig);
  main.addSeparator();

  // ── Config / Tools ───────────────────────────────────────────────────────
  const cfgMenu = ui.createMenu('Config / Tools');
  cfgMenu.addItem('Ensure Config UI',      'ensureConfigUI');
  cfgMenu.addItem('Validate Config',       'validateConfigAndReport_');
  cfgMenu.addItem('Validate attachments',  'validateAttachments_');
  cfgMenu.addItem('About (version)',       'aboutModal_');
  main.addSubMenu(cfgMenu);

  main.addToUi();
}

// Named shims so GAS menu registration works for the dynamic template items.
// One function per template — mirroring the pattern above.
function _loadTemplate_Annual_Fire_Inspection()  { loadTemplate_('Annual Fire Inspection');  }
function _loadTemplate_Water_Outage()            { loadTemplate_('Water Outage');            }
function _loadTemplate_General_Maintenance()     { loadTemplate_('General Maintenance');     }
function _loadTemplate_Weather_Alert()           { loadTemplate_('Weather Alert');           }
function _loadTemplate_Power_Outage()            { loadTemplate_('Power Outage');            }
function _loadTemplate_WiFi_Outage()             { loadTemplate_('WiFi Outage');             }

// ── About ─────────────────────────────────────────────────────────────────────

function aboutModal_() {
  showHtmlOrDraft_(`
    <div style="font-family:Arial,Helvetica,sans-serif;padding:12px;">
      <h2 style="margin:0 0 8px;">Mass Notifications</h2>
      <div>Version: <code>${VERSION}</code></div>
      <div>Recipients sheet: <code>${DEFAULT_RECIPIENTS_SHEET}</code></div>
      <div>Config sheet: <code>${CONFIG_SHEET}</code></div>
      <div>Run Log sheet: <code>${RUN_LOG_SHEET}</code></div>
      <div style="margin-top:10px;font-size:12px;color:#555;">
        Available cards: ${Object.keys(CARD_REGISTRY).join(', ')}<br>
        Available templates: ${Object.keys(EMAIL_TEMPLATES).join(', ')}
      </div>
    </div>`, 'About Mass Notifications');
}

// ── UI helpers ────────────────────────────────────────────────────────────────

/**
 * Shows an HTML modal dialog in the spreadsheet UI.
 * Falls back to creating a Gmail draft when there is no UI context (e.g. triggered runs).
 */
function showHtmlOrDraft_(html, title) {
  const output = HtmlService.createHtmlOutput(html).setWidth(900).setHeight(750);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    if (!ss) throw new Error('No active spreadsheet container.');
    SpreadsheetApp.getUi().showModalDialog(output, title);
  } catch (e) {
    const msg = String(e && e.message || e);
    const isHeadless =
      msg.includes('Cannot call SpreadsheetApp.getUi') ||
      msg.includes('No active spreadsheet') ||
      msg.includes('No active spreadsheet container') ||
      msg.includes('There is no active container');

    if (isHeadless) {
      const dest = Session.getActiveUser().getEmail();
      GmailApp.createDraft(dest, `PREVIEW – ${title}`, '', { htmlBody: html });
      Logger.log(`No UI context. Preview written to Gmail draft for ${dest}`);
      return;
    }

    try { SpreadsheetApp.getActive().toast('Preview failed: ' + msg); } catch (_) {}
    throw e;
  }
}

/**
 * Shows an alert dialog; falls back to Logger.log when UI is unavailable.
 */
function safeAlert_(msg) {
  try { SpreadsheetApp.getUi().alert(msg); } catch (_) { Logger.log(msg); }
}

/**
 * Shows an OK/Cancel dialog and returns true if the operator clicked OK.
 * Returns true when running without UI (headless triggers).
 */
function confirmProceed_(title, msg) {
  try {
    const ui  = SpreadsheetApp.getUi();
    const res = ui.alert(title, msg, ui.ButtonSet.OK_CANCEL);
    return res === ui.Button.OK;
  } catch (_) {
    return true;
  }
}

/* Optional: quick sanity check for Gmail Advanced Service */
function testGmailAdvanced() {
  const resp = Gmail.Users.Settings.SendAs.list('me');
  Logger.log(resp && resp.sendAs ? resp.sendAs.length : 'no sendAs');
}
