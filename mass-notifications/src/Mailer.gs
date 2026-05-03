/**
 * Mass Notifications — Mailer
 * Core send logic for INDIVIDUAL and BCC modes.
 * Entry point: sendMassNotifications() (triggered by menu).
 */

// ── Entry point ───────────────────────────────────────────────────────────────

function sendMassNotifications() {
  const cfg = loadConfig_();
  const v   = validateConfig_(cfg);

  if (v.errors.length) {
    showHtmlOrDraft_(renderValidationHtml_(v, true), 'Config Validation (blocking)');
    return;
  }

  if (v.warnings.length) {
    const proceed = confirmProceed_(
      'Config warnings',
      `Found ${v.warnings.length} warning(s). Proceed with send?`
    );
    if (!proceed) {
      showHtmlOrDraft_(renderValidationHtml_(v, false), 'Config Validation (warnings)');
      return;
    }
  }

  const mode = String(cfg.sendMode).toUpperCase();
  if (mode === 'BCC')      return sendModeBcc_(cfg);
  if (mode === 'MOVE_IN')  return sendModeMoveIn_(cfg);
  return sendModeIndividual_(cfg);
}

// ── Validation HTML ───────────────────────────────────────────────────────────

function renderValidationHtml_(v, blocking) {
  const errs  = v.errors?.length
    ? `<h3>Errors</h3><ul><li>${v.errors.join('</li><li>')}</li></ul>`
    : '';
  const warns = v.warnings?.length
    ? `<h3>Warnings</h3><ul><li>${v.warnings.join('</li><li>')}</li></ul>`
    : '<p>No warnings.</p>';
  const hdr = `<h2 style="margin:0 0 6px;">Config ${blocking ? 'Validation — Blocking' : 'Validation — Warnings'}</h2>`;
  return `<div style="font-family:Arial,Helvetica,sans-serif;padding:10px;">${hdr}${errs}${warns}</div>`;
}

// ── INDIVIDUAL mode ───────────────────────────────────────────────────────────

/**
 * Sends one personalised email per eligible recipient row.
 * Logs BEFORE/AFTER state to Run_Log and marks each row SENT.
 *
 * @param {Object} cfg — result of loadConfig_()
 */
function sendModeIndividual_(cfg) {
  const sh         = getRecipientsSheet_(cfg);
  const maxPerRun  = Number(cfg.maxPerRun || 500);
  const targets    = getTargetRows_(sh, maxPerRun);

  if (!targets.length) return safeAlert_('No eligible rows (check Email/Status).');

  const subjectPreview = renderWithTokens_(cfg.subjectTemplate, {
    ...buildPerRowTokens_(cfg, { email: '', name: '', unit: '' }),
    first_name: '<first>',
    unit:       '<unit>',
  });

  const run = logRunStart_({
    mode:    'SEND_INDIVIDUAL',
    cfg,     shName:  sh.getName(),
    subject: subjectPreview,
    rows:    targets.map(t => t.row),
    emails:  targets.map(t => t.email),
  });

  const cfgAttachIds = parseIdList_(cfg.attachmentFileIds);
  let   sent = 0;

  try {
    for (const t of targets) {
      const tokens  = buildPerRowTokens_(cfg, t);
      const subject = renderWithTokens_(cfg.subjectTemplate, tokens);
      const body    = buildHtmlBody_(cfg, tokens, t.unit);

      // Resolve attachments (config + row)
      const rowIds     = parseIdList_(String(sh.getRange(t.row, COL.ATTACH_IDS).getValue() || ''));
      const allIds     = [...cfgAttachIds, ...rowIds];
      let   attachments = [];

      if (allIds.length) {
        try {
          attachments = getBlobsForIds_(allIds, { exportGoogleToPdf: true, maxBytes: MAX_ATTACH_BYTES });
        } catch (e) {
          sh.getRange(t.row, COL.STATUS).setValue('REVIEW');
          sh.getRange(t.row, COL.NOTES).setValue(e.message);
          run.rowAfter.push({ row: t.row, status: 'REVIEW', lastSent: null });
          continue;
        }
      }

      GmailApp.sendEmail(t.email, subject, '', {
        cc:          combineCc_(cfg),
        bcc:         cfg.bccExtra || '',
        replyTo:     cfg.replyTo  || '',
        htmlBody:    body,
        name:        cfg.senderDisplayName || 'Landing Notifications',
        attachments,
      });

      const now = new Date();
      sh.getRange(t.row, COL.STATUS).setValue('SENT');
      sh.getRange(t.row, COL.LAST_SENT).setValue(now);
      run.rowAfter.push({ row: t.row, status: 'SENT', lastSent: now });

      sent++;
      Utilities.sleep(100); // gentle throttle
    }

    logRunComplete_(run, sent, null);
    safeAlert_(`Sent ${sent} individualised email(s).`);
  } catch (e) {
    logRunComplete_(run, sent, String(e && e.message || e));
    throw e;
  }
}

// ── BCC mode ──────────────────────────────────────────────────────────────────

/**
 * Sends in batches with all recipients in BCC.
 * No per-recipient personalisation; uses config-level attachments only.
 *
 * @param {Object} cfg — result of loadConfig_()
 */
function sendModeBcc_(cfg) {
  const sh             = getRecipientsSheet_(cfg);
  const { emails, rows } = collectEmailsForBcc_(sh);

  if (!emails.length) return safeAlert_('No eligible recipients for BCC (check Email/Status).');

  const tokens  = { ...buildGlobalTokens_(cfg), first_name: 'Residents', member_name: '', member_email: '', unit: '' };
  const subject = renderWithTokens_(cfg.subjectTemplate, tokens);
  const body    = buildHtmlBody_(cfg, tokens, null);

  const run = logRunStart_({
    mode: 'SEND_BCC',
    cfg,  shName:  sh.getName(),
    subject, rows, emails,
  });

  // Config-level attachments only
  const cfgIds = parseIdList_(cfg.attachmentFileIds);
  let attachments = [];
  if (cfgIds.length) {
    try {
      attachments = getBlobsForIds_(cfgIds, { exportGoogleToPdf: true, maxBytes: MAX_ATTACH_BYTES });
    } catch (e) {
      return safeAlert_('Config attachments failed: ' + e.message);
    }
  }

  const batchSize = Number(cfg.batchSize || 90);
  const now       = new Date();
  let   totalSent = 0;

  try {
    for (let i = 0; i < emails.length; i += batchSize) {
      const slice = emails.slice(i, i + batchSize);
      GmailApp.sendEmail('', subject, '', {
        cc:          combineCc_(cfg),
        bcc:         [slice.join(','), cfg.bccExtra || ''].filter(Boolean).join(','),
        replyTo:     cfg.replyTo || '',
        htmlBody:    body,
        name:        cfg.senderDisplayName || 'Landing Notifications',
        attachments,
      });
      totalSent += slice.length;
      Utilities.sleep(300);
    }

    rows.forEach(r => {
      sh.getRange(r, COL.STATUS).setValue('SENT');
      sh.getRange(r, COL.LAST_SENT).setValue(now);
      run.rowAfter.push({ row: r, status: 'SENT', lastSent: now });
    });

    logRunComplete_(run, totalSent, null);
    safeAlert_(`Sent to ${totalSent} recipient(s) in BCC mode.`);
  } catch (e) {
    logRunComplete_(run, totalSent, String(e && e.message || e));
    throw e;
  }
}

// ── MOVE_IN mode ──────────────────────────────────────────────────────────────

/**
 * Sends one email per Move-In Flow row to that row's property contacts,
 * with the member's background check + ID scan attached. The body is
 * rendered from the Move-In Notification template (per-row card with
 * apartment/member/vehicle/pet/occupants/area-manager block).
 *
 * Recipients are taken from PROPERTY_EMAIL (comma-separated). manager_email
 * is intentionally NOT CC'd — the per-row area manager is in the body, and
 * Move-In is a property-facing B2B email rather than a member notification.
 *
 * @param {Object} cfg — result of loadConfig_()
 */
function sendModeMoveIn_(cfg) {
  const sh         = getMoveInSheet_(cfg);
  const maxPerRun  = Number(cfg.maxPerRun || 500);
  const targets    = getMoveInTargetRows_(sh, maxPerRun);

  if (!targets.length) return safeAlert_('No eligible Move-In rows (check Property Email/Status).');

  const subjectPreview = renderWithTokens_(cfg.subjectTemplate, {
    ...buildMoveInTokens_(cfg, targets[0]),
    member_name:      '<member>',
    apartment_number: '<apt>',
  });

  const run = logRunStart_({
    mode:    'SEND_MOVE_IN',
    cfg,     shName:  sh.getName(),
    subject: subjectPreview,
    rows:    targets.map(t => t.row),
    emails:  targets.map(t => t.propertyEmails.join(',')),
    schema:  moveInRunSchema_(),
  });

  const cfgAttachIds = parseIdList_(cfg.attachmentFileIds);
  let   sent = 0;

  try {
    for (const t of targets) {
      const tokens  = buildMoveInTokens_(cfg, t);
      const subject = renderWithTokens_(cfg.subjectTemplate, tokens);
      const body    = buildHtmlBody_(cfg, tokens, null);

      const rowIds = parseIdList_(t.attachIds);
      const allIds = [...cfgAttachIds, ...rowIds];
      let   attachments = [];

      if (allIds.length) {
        try {
          attachments = getBlobsForIds_(allIds, { exportGoogleToPdf: true, maxBytes: MAX_ATTACH_BYTES });
        } catch (e) {
          sh.getRange(t.row, MOVEIN_COL.STATUS).setValue('REVIEW');
          sh.getRange(t.row, MOVEIN_COL.NOTES ).setValue(e.message);
          run.rowAfter.push({ row: t.row, status: 'REVIEW', lastSent: null });
          continue;
        }
      }

      GmailApp.sendEmail(t.propertyEmails.join(','), subject, '', {
        cc:          cfg.ccExtra  || '',
        bcc:         cfg.bccExtra || '',
        replyTo:     cfg.replyTo  || '',
        htmlBody:    body,
        name:        cfg.senderDisplayName || 'Landing Notifications',
        attachments,
      });

      const now = new Date();
      sh.getRange(t.row, MOVEIN_COL.STATUS   ).setValue('SENT');
      sh.getRange(t.row, MOVEIN_COL.LAST_SENT).setValue(now);
      run.rowAfter.push({ row: t.row, status: 'SENT', lastSent: now });

      sent++;
      Utilities.sleep(100);
    }

    logRunComplete_(run, sent, null);
    safeAlert_(`Sent ${sent} Move-In notification(s) to property contacts.`);
  } catch (e) {
    logRunComplete_(run, sent, String(e && e.message || e));
    throw e;
  }
}
