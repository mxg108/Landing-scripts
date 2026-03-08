/**
 * Mass Notifications — DryRun
 * All preview, draft-creation, and test-send functions.
 * Nothing here marks rows SENT or modifies the recipients sheet
 * (except dryRunCreateDrafts, which marks rows DRAFT).
 */

// ── Quick count ───────────────────────────────────────────────────────────────

function previewRecipients() {
  const { emails, managerEmail } = collectEmailsForCount_();
  safeAlert_(
    `Manager (CC): ${managerEmail || '(none)'}\n` +
    `Eligible recipients: ${emails.length}`
  );
}

// ── INDIVIDUAL dry runs ───────────────────────────────────────────────────────

function dryRunPreview() {
  const cfg     = loadConfig_();
  const sh      = getRecipientsSheet_(cfg);
  const limit   = Number(cfg.dryRunLimit || 10);
  const targets = getTargetRows_(sh, limit);
  if (!targets.length) return safeAlert_('No target rows (check Email/Status).');

  const cfgAttachIds = parseIdList_(cfg.attachmentFileIds);

  const cards = targets.map(t => {
    const tokens   = buildPerRowTokens_(cfg, t);
    const subject  = renderWithTokens_(cfg.subjectTemplate, tokens);
    const htmlBody = buildHtmlBody_(cfg, tokens, t.unit);

    const rowIds    = parseIdList_(String(sh.getRange(t.row, COL.ATTACH_IDS).getValue() || ''));
    const allIds    = [...cfgAttachIds, ...rowIds];
    const { names, notes } = summarizeAttachmentNames_(allIds);

    const attachHtml = allIds.length
      ? `<div><strong>Attachments (${allIds.length}):</strong><br>${names.map(n => escapeHtml_(n)).join('<br>')}</div>`
      : `<div><strong>Attachments:</strong> (none)</div>`;

    const warnHtml = notes.length
      ? `<div style="color:#a33;margin-top:6px;"><strong>Attachment issues:</strong><br>${notes.map(n => escapeHtml_(n)).join('<br>')}</div>`
      : '';

    const safeBody = makePreviewSafe_(htmlBody);

    return `
      <div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0;">
        <div><strong>To:</strong> ${escapeHtml_(t.email)}</div>
        <div><strong>CC:</strong> ${escapeHtml_(combineCc_(cfg) || '(none)')}</div>
        <div><strong>Subject:</strong> ${escapeHtml_(subject)}</div>
        ${attachHtml}${warnHtml}
        <div style="margin-top:8px;color:#666;font-size:12px;">
          Preview note: complex signature/table content may be collapsed here but will render in the actual email.
        </div>
        <hr style="margin:10px 0;">
        <div>${safeBody}</div>
      </div>`;
  }).join('');

  showHtmlOrDraft_(`
    <div style="font-family:Arial,Helvetica,sans-serif;padding:10px;">
      <h2 style="margin:0 0 6px;">Dry Run – Individual Preview</h2>
      <div style="color:#555;margin-bottom:12px;">Showing the first ${targets.length} of ${limit}. No emails were sent.</div>
      ${cards}
    </div>`, 'Dry Run – Individual Preview');
}


function dryRunCreateDrafts() {
  const cfg     = loadConfig_();
  const sh      = getRecipientsSheet_(cfg);
  const limit   = Number(cfg.dryRunLimit || 10);
  const targets = getTargetRows_(sh, limit);
  if (!targets.length) return safeAlert_('No target rows (check Email/Status).');

  const run = logRunStart_({
    mode: 'DRYRUN_DRAFTS', cfg, shName: sh.getName(),
    subject: '(drafts)',
    rows:   targets.map(t => t.row),
    emails: targets.map(t => t.email),
  });

  const cfgAttachIds = parseIdList_(cfg.attachmentFileIds);
  let created = 0;

  for (const t of targets) {
    const tokens   = buildPerRowTokens_(cfg, t);
    const subject  = '[DRAFT] ' + renderWithTokens_(cfg.subjectTemplate, tokens);
    const htmlBody = buildHtmlBody_(cfg, tokens, t.unit);

    const rowIds = parseIdList_(String(sh.getRange(t.row, COL.ATTACH_IDS).getValue() || ''));
    const allIds = [...cfgAttachIds, ...rowIds];
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

    GmailApp.createDraft(t.email, subject, '', {
      cc: combineCc_(cfg), replyTo: cfg.replyTo || '',
      htmlBody, name: cfg.senderDisplayName || 'Landing Notifications',
      attachments,
    });

    const now = new Date();
    sh.getRange(t.row, COL.STATUS).setValue('DRAFT');
    sh.getRange(t.row, COL.LAST_SENT).setValue(now);
    run.rowAfter.push({ row: t.row, status: 'DRAFT', lastSent: now });
    created++;
    Utilities.sleep(100);
  }

  logRunComplete_(run, created, null);
  safeAlert_(`Created ${created} Gmail draft(s). No emails were sent.`);
}


function dryRunTestSendToMe() {
  const cfg     = loadConfig_();
  const sh      = getRecipientsSheet_(cfg);
  const targets = getTargetRows_(sh, 1);
  if (!targets.length) return safeAlert_('No eligible rows found (check Email/Status).');

  const t        = targets[0];
  const tokens   = buildPerRowTokens_(cfg, t);
  const subject  = 'TEST — ' + renderWithTokens_(cfg.subjectTemplate, tokens);
  const body     = buildHtmlBody_(cfg, tokens, t.unit);

  const cfgIds = parseIdList_(cfg.attachmentFileIds);
  const rowIds = parseIdList_(String(sh.getRange(t.row, COL.ATTACH_IDS).getValue() || ''));
  const allIds = [...cfgIds, ...rowIds];
  let   attachments = [];

  if (allIds.length) {
    try {
      attachments = getBlobsForIds_(allIds, { exportGoogleToPdf: true, maxBytes: MAX_ATTACH_BYTES });
    } catch (e) {
      return safeAlert_('Attachments error for test row: ' + e.message);
    }
  }

  const preface = `
    <div style="border:1px dashed #aaa;padding:10px;margin-bottom:12px;font-family:Arial,Helvetica,sans-serif;">
      <div><strong>Simulated To:</strong> ${escapeHtml_(t.email)}</div>
      ${combineCc_(cfg) ? `<div><strong>Simulated CC:</strong> ${escapeHtml_(combineCc_(cfg))}</div>` : ''}
      ${cfg.bccExtra    ? `<div><strong>Simulated BCC (extra):</strong> ${escapeHtml_(cfg.bccExtra)}</div>` : ''}
    </div>`;

  const dest = (cfg.testEmail && String(cfg.testEmail).trim()) || Session.getActiveUser().getEmail();
  GmailApp.sendEmail(dest, subject, '', {
    htmlBody: preface + body,
    replyTo:  cfg.replyTo || '',
    name:     cfg.senderDisplayName || 'Landing Notifications',
    attachments,
  });

  safeAlert_(`Test email sent to ${dest}.`);
}

// ── BCC dry runs ──────────────────────────────────────────────────────────────

function dryRunPreviewBCC() {
  const cfg        = loadConfig_();
  const sh         = getRecipientsSheet_(cfg);
  const { emails } = collectEmailsForBcc_(sh);
  if (!emails.length) return safeAlert_('No eligible recipients for BCC (check Email/Status).');

  const batchSize  = Number(cfg.batchSize || 90);
  const total      = emails.length;
  const batches    = Math.ceil(total / batchSize);
  const firstBatch = emails.slice(0, batchSize);

  const tokens  = { ...buildGlobalTokens_(cfg), first_name: 'Residents', member_name: '', member_email: '', unit: '' };
  const subject = renderWithTokens_(cfg.subjectTemplate, tokens);
  const body    = buildHtmlBody_(cfg, tokens, null);

  const warnings = [];
  const tmplText = [
    cfg.greetingTemplate, cfg.bodyIntroHtml, cfg.disclaimerHtml,
    cfg.closingHtml, cfg.bodyFullHtml, cfg.subjectTemplate,
  ].join(' ');
  if (cfg.includeUnitLine) warnings.push('Include Unit is ON but BCC mode cannot personalise per recipient; unit line will be omitted.');
  if (/\{\{\s*(first_name|member_name|unit)\b/i.test(tmplText))
    warnings.push('Templates include per-recipient tokens; in BCC mode these are replaced by generic values.');

  const warnHtml = warnings.length
    ? `<div style="background:#fff5cc;border:1px solid #f1d36b;color:#7a5b00;padding:10px;border-radius:6px;margin-bottom:10px;">
         <strong>Heads-up:</strong><br>${warnings.map(w => `• ${escapeHtml_(w)}`).join('<br>')}
       </div>` : '';

  showHtmlOrDraft_(`
    <div style="font-family:Arial,Helvetica,sans-serif;padding:10px;">
      <h2 style="margin:0 0 6px;">Dry Run – BCC Preview</h2>
      ${warnHtml}
      <div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0;">
        <div><strong>Mode:</strong> BCC</div>
        <div><strong>Total recipients:</strong> ${total}</div>
        <div><strong>Batch size:</strong> ${batchSize} &nbsp; <strong>Batches:</strong> ${batches}</div>
        <div><strong>CC (visible):</strong> ${escapeHtml_(combineCc_(cfg) || '(none)')}</div>
        <div><strong>Extra BCC:</strong> ${escapeHtml_(cfg.bccExtra || '(none)')}</div>
        <div style="margin-top:8px;"><strong>First batch (up to ${firstBatch.length}):</strong><br>${escapeHtml_(firstBatch.join(', '))}</div>
        <hr style="margin:12px 0;">
        <div><strong>Subject:</strong> ${escapeHtml_(subject)}</div>
        <hr style="margin:12px 0;">
        <div>${makePreviewSafe_(body)}</div>
      </div>
    </div>`, 'Dry Run – BCC Preview');
}


function dryRunCreateDraftsBCC() {
  const cfg        = loadConfig_();
  const sh         = getRecipientsSheet_(cfg);
  const { emails } = collectEmailsForBcc_(sh);
  if (!emails.length) return safeAlert_('No eligible recipients for BCC (check Email/Status).');

  const batchSize = Number(cfg.batchSize || 90);
  const slice     = emails.slice(0, batchSize);
  const tokens    = { ...buildGlobalTokens_(cfg), first_name: 'Residents', member_name: '', member_email: '', unit: '' };
  const subject   = '[DRAFT] ' + renderWithTokens_(cfg.subjectTemplate, tokens);
  const body      = buildHtmlBody_(cfg, tokens, null);

  GmailApp.createDraft('', subject, '', {
    cc: combineCc_(cfg),
    bcc: [slice.join(','), cfg.bccExtra || ''].filter(Boolean).join(','),
    replyTo: cfg.replyTo || '', htmlBody: body,
    name: cfg.senderDisplayName || 'Landing Notifications',
  });

  safeAlert_(`Draft created for first BCC batch (${slice.length} recipients).`);
}


function dryRunTestSendToMeBCC() {
  const cfg        = loadConfig_();
  const sh         = getRecipientsSheet_(cfg);
  const { emails } = collectEmailsForBcc_(sh);
  if (!emails.length) return safeAlert_('No eligible recipients for BCC (check Email/Status).');

  const batchSize = Number(cfg.batchSize || 90);
  const slice     = emails.slice(0, Math.min(emails.length, batchSize));
  const tokens    = { ...buildGlobalTokens_(cfg), first_name: 'Residents', member_name: '', member_email: '', unit: '' };
  const subject   = 'TEST — ' + renderWithTokens_(cfg.subjectTemplate, tokens);
  const body      = buildHtmlBody_(cfg, tokens, null);

  const preface = `
    <div style="border:1px dashed #aaa;padding:10px;margin-bottom:12px;font-family:Arial,Helvetica,sans-serif;">
      <div><strong>Simulated CC:</strong> ${escapeHtml_(combineCc_(cfg) || '(none)')}</div>
      <div><strong>Simulated extra BCC:</strong> ${escapeHtml_(cfg.bccExtra || '(none)')}</div>
      <div><strong>Simulated BCC list (first batch):</strong><br>${escapeHtml_(slice.join(', '))}</div>
    </div>`;

  const dest = (cfg.testEmail && String(cfg.testEmail).trim()) || Session.getActiveUser().getEmail();
  GmailApp.sendEmail(dest, subject, '', {
    htmlBody: preface + body, replyTo: cfg.replyTo || '',
    name: cfg.senderDisplayName || 'Landing Notifications',
  });

  safeAlert_(`BCC test email sent to ${dest}.`);
}
