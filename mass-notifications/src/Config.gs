/**
 * Mass Notifications — Config
 * Reads and writes the Config sheet, validates keys, and exposes ensureConfigUI()
 * to set up dropdowns/date pickers on first run.
 */

// All keys the script recognises — used by validateConfig_ to warn about typos.
const KNOWN_CONFIG_KEYS = [
  'recipients_sheet_name', 'send_mode',
  'manager_email', 'manager_name', 'cc_extra', 'bcc_extra',
  'sender_display_name', 'reply_to', 'test_email',
  'property_name', 'event_name',
  'subject_template', 'greeting_template',
  'include_disclaimer', 'disclaimer_html',
  'body_intro_html', 'include_unit_line', 'closing_html',
  'body_full_html', 'signature_html',
  'notification_card',
  'timezone', 'window_start', 'window_end',
  'dry_run_limit', 'max_per_run', 'batch_size',
  'attachment_file_ids',
];

// ── Config loading ────────────────────────────────────────────────────────────

/**
 * Reads the Config sheet and returns a flat configuration object.
 * All callers should call this once and pass the result around.
 */
function loadConfig_() {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(CONFIG_SHEET);
  if (!sh) throw new Error(`Missing sheet: ${CONFIG_SHEET}`);

  const rows = sh.getRange(1, 1, Math.max(2, sh.getLastRow()), 2).getValues();
  const kv   = {};
  rows.forEach(([k, v]) => {
    if (k) kv[String(k).trim()] = typeof v === 'string' ? v.trim() : v;
  });

  const tz = kv['timezone'] || Session.getScriptTimeZone() || 'America/Mexico_City';
  const { start, end } = computeWindow_(kv['window_start'], kv['window_end'], tz);

  return {
    timezone:            tz,
    start,
    end,
    recipientsSheetName: kv['recipients_sheet_name'] || DEFAULT_RECIPIENTS_SHEET,
    sendMode:            (kv['send_mode'] || 'INDIVIDUAL').toUpperCase(),
    managerEmail:        kv['manager_email']  || '',
    managerName:         (kv['manager_name'] && String(kv['manager_name']).trim())
                           || deriveNameFromEmail_(kv['manager_email'] || ''),
    ccExtra:             kv['cc_extra']   || '',
    bccExtra:            kv['bcc_extra']  || '',
    senderDisplayName:   kv['sender_display_name'] || 'Landing Notifications',
    replyTo:             kv['reply_to']   || '',
    testEmail:           kv['test_email'] || '',
    propertyName:        kv['property_name'] || '',
    eventName:           kv['event_name']    || '',
    subjectTemplate:     kv['subject_template']
                           || 'Notice: {{event_name}} — {{property_name}} ({{date_range}})',
    greetingTemplate:    kv['greeting_template'] || '<p>Dear {{first_name | Resident}},</p>',
    includeDisclaimer:   toBool_(kv['include_disclaimer'], false),
    disclaimerHtml:      kv['disclaimer_html'] || '',
    bodyIntroHtml:       kv['body_intro_html'] || '',
    includeUnitLine:     toBool_(kv['include_unit_line'], true),
    closingHtml:         kv['closing_html']
                           || '<p>Thank you for your cooperation and understanding.</p>',
    bodyFullHtml:        kv['body_full_html']  || '',
    signatureHtml:       kv['signature_html']  || '',
    notificationCard:    (kv['notification_card'] || '').trim().toUpperCase(),
    dryRunLimit:         Number(kv['dry_run_limit'] || 10),
    maxPerRun:           Number(kv['max_per_run']   || 500),
    batchSize:           Number(kv['batch_size']    || 90),
    attachmentFileIds:   (kv['attachment_file_ids'] || '').trim(),
  };
}

// ── Config writing ────────────────────────────────────────────────────────────

/**
 * Sets a single key in the Config sheet, appending a new row if the key
 * does not already exist.
 */
function setConfigValue_(key, value) {
  const sh   = SpreadsheetApp.getActive().getSheetByName(CONFIG_SHEET);
  const last = Math.max(2, sh.getLastRow());
  for (let r = 2; r <= last; r++) {
    if (String(sh.getRange(r, 1).getValue()).trim() === key) {
      sh.getRange(r, 2).setValue(value);
      return;
    }
  }
  sh.appendRow([key, value]);
}

// ── Validation ────────────────────────────────────────────────────────────────

/**
 * Checks config keys and common pitfalls.
 * Returns { errors: string[], warnings: string[] }.
 * Errors are blocking; warnings prompt the operator to confirm.
 */
function validateConfig_(cfg) {
  const errors   = [];
  const warnings = [];

  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(CONFIG_SHEET);
  if (!sh) { errors.push('Missing Config sheet.'); return { errors, warnings }; }

  // Warn about unrecognised keys (likely typos)
  const dataRows = sh.getLastRow() - 1;
  if (dataRows < 1) return { errors, warnings };
  const rows = sh.getRange(2, 1, dataRows, 2).getValues();
  rows.forEach(([k]) => {
    if (!k) return;
    if (!KNOWN_CONFIG_KEYS.includes(String(k).trim())) {
      warnings.push(`Unknown key in Config: <code>${escapeHtml_(k)}</code> (ignored)`);
    }
  });

  if (!cfg.propertyName) warnings.push('Recommended: set <code>property_name</code>.');
  if (!cfg.eventName)    warnings.push('Recommended: set <code>event_name</code>.');

  // BCC mode + per-recipient tokens
  const tmplText = [
    cfg.greetingTemplate, cfg.bodyIntroHtml, cfg.disclaimerHtml,
    cfg.closingHtml, cfg.bodyFullHtml, cfg.subjectTemplate,
  ].join(' ');
  if (String(cfg.sendMode).toUpperCase() === 'BCC' &&
      /\{\{\s*(first_name|member_name|unit)\b/i.test(tmplText)) {
    warnings.push(
      'In BCC mode, per-recipient tokens (<code>first_name</code>, <code>member_name</code>, ' +
      '<code>unit</code>) will be replaced by generic values.'
    );
  }

  return { errors, warnings };
}

/** Menu entry: runs validateConfig_ and shows the result in a modal. */
function validateConfigAndReport_() {
  const cfg = loadConfig_();
  const v   = validateConfig_(cfg);
  showHtmlOrDraft_(renderValidationHtml_(v, v.errors.length > 0), 'Config Validation');
}

// ── Config UI setup ───────────────────────────────────────────────────────────

/**
 * Ensures the Config sheet exists with all required rows, dropdowns, and
 * date pickers.  Safe to re-run at any time.
 */
function ensureConfigUI() {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(CONFIG_SHEET) || ss.insertSheet(CONFIG_SHEET);

  if (String(sh.getRange('A1').getValue()).trim() !== 'Key') {
    sh.getRange('A1').setValue('Key');
    sh.getRange('B1').setValue('Value');
  }

  function ensureRow(key, defVal) {
    const last = Math.max(2, sh.getLastRow());
    for (let r = 2; r <= last; r++) {
      if (String(sh.getRange(r, 1).getValue()).trim() === key) {
        if (defVal != null && String(sh.getRange(r, 2).getValue()).trim() === '') {
          sh.getRange(r, 2).setValue(defVal);
        }
        return r;
      }
    }
    sh.appendRow([key, defVal == null ? '' : defVal]);
    return sh.getLastRow();
  }

  const rMode     = ensureRow('send_mode',           'INDIVIDUAL');
  const rDisc     = ensureRow('include_disclaimer',  'YES');
  const rUnit     = ensureRow('include_unit_line',   'YES');
  const rStart    = ensureRow('window_start',        '');
  const rEnd      = ensureRow('window_end',          '');
  const rCard     = ensureRow('notification_card',   '');
                    ensureRow('body_full_html',       '');
                    ensureRow('attachment_file_ids',  '');
                    ensureRow('manager_name',         '');

  const dvMode    = SpreadsheetApp.newDataValidation()
                      .requireValueInList(['INDIVIDUAL', 'BCC'], true).build();
  const dvYesNo   = SpreadsheetApp.newDataValidation()
                      .requireValueInList(['YES', 'NO'], true).build();
  const dvDate    = SpreadsheetApp.newDataValidation().requireDate().build();
  const dvCard    = SpreadsheetApp.newDataValidation()
                      .requireValueInList(
                        ['', ...Object.keys(CARD_REGISTRY)], true
                      ).build();

  sh.getRange(rMode,  2).setDataValidation(dvMode);
  sh.getRange(rDisc,  2).setDataValidation(dvYesNo);
  sh.getRange(rUnit,  2).setDataValidation(dvYesNo);
  sh.getRange(rStart, 2).setDataValidation(dvDate).setNumberFormat('yyyy-mm-dd');
  sh.getRange(rEnd,   2).setDataValidation(dvDate).setNumberFormat('yyyy-mm-dd');
  sh.getRange(rCard,  2).setDataValidation(dvCard);

  sh.setColumnWidths(1, 1, 220);
  sh.setColumnWidths(2, 1, 1000);
  sh.getRange('A1:B1').setFontWeight('bold').setBackground('#f1f3f4');

  safeAlert_('Config UI ensured.');
}
