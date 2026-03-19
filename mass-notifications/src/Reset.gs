/**
 * Mass Notifications — Reset / Archive / Undo / Signature
 * Operations that manage the lifecycle of a send campaign:
 * archiving past recipients, resetting statuses, full reset for a new campaign,
 * undoing the last run, and syncing the Gmail signature.
 */

// ── Archive & clear ───────────────────────────────────────────────────────────

/**
 * Copies the recipients sheet to a timestamped hidden archive tab,
 * then clears all data rows (keeps headers).
 * Shows a confirmation alert when done.
 */
function archiveAndClearRecipients() {
  const sheetName = archiveAndClearRecipientsQuiet_();
  safeAlert_(`Archived "${sheetName}" to database and cleared recipient rows.`);
}

/**
 * Same as archiveAndClearRecipients() but shows no alert.
 * Used by the Looker sync pipeline so it doesn't interrupt the flow
 * with an intermediate dialog.
 *
 * @return {string} The name of the recipients sheet that was archived.
 */
function archiveAndClearRecipientsQuiet_() {
  const cfg  = loadConfig_();
  const sh   = getRecipientsSheet_(cfg);
  const last = sh.getLastRow();

  // Archive all rows (sent + skipped) to PostgreSQL
  if (last >= 2) {
    const data = sh.getRange(2, 1, last - 1, COL_MAX).getValues();
    try {
      archiveCampaignToDb_(cfg, sh.getName(), data);
    } catch (e) {
      // If DB write fails, log the error but don't block the reset.
      // The Run_Log still has RowStates as a fallback.
      Logger.log('DB archive failed (data preserved in Run_Log): ' + e.message);
    }
  }

  // Clear all data rows (keep headers)
  if (last >= 2) sh.getRange(2, 1, last - 1, COL_MAX).clearContent();

  return sh.getName();
}

/**
 * Clears only Status and Last Sent columns — leaves email/name/unit intact.
 */
function resetStatusesOnly() {
  const cfg  = loadConfig_();
  const sh   = getRecipientsSheet_(cfg);
  const last = sh.getLastRow();
  if (last >= 2) {
    sh.getRange(2, COL.STATUS,    last - 1, 1).clearContent();
    sh.getRange(2, COL.LAST_SENT, last - 1, 1).clearContent();
  }
  safeAlert_('Statuses and "Last Sent" cleared.');
}

// ── Full reset ────────────────────────────────────────────────────────────────

/**
 * Archives + clears recipients, then restores all config keys to safe defaults.
 * Run-specific fields (property_name, manager_email, dates, …) are blanked so
 * the operator must fill them in before the next send.
 */
function fullResetForNextUse() {
  archiveAndClearRecipients();

  const genericSubject =
    'Notice: {{event_name | Property Needs Access}} — {{property_name}} ({{date_range}})';

  const genericIntro =
    '<p>This is a courtesy notice for <strong>{{event_name}}</strong> at {{property_name}} ' +
    'between <strong>{{date_range}}</strong>. Please ensure your unit is accessible.</p>';

  const genericClosing =
    '<p>If you have any questions or need assistance, please call or text your General Manager ' +
    '{{manager_name}} or our 24/7 Member Support Line at ' +
    '<a href="tel:' + LANDING_IVR_PHONE + '">' + LANDING_IVR_PHONE_DISPLAY + '</a> and we will be happy to help.</p>' +
    '<p>Warm regards,<br>The Landing Team</p>';

  const genericDisclaimer =
    '<div style="text-align:center;background-color:#E7EFFB;border:2.5px solid #15192D;' +
    'border-radius:6px;padding:10px;color:#15192D;font-style:italic;">' +
    'This is a notification to all active residents at {{property_name}}. ' +
    'Please see the message below:</div>';

  const defaultSignature =
    '<div dir="ltr"><table cellpadding="0" cellspacing="0" border="0" style="font-family:system-ui,-apple-system,&quot;Segoe UI&quot;,Helvetica,Arial,sans-serif;color:rgb(21,25,45);line-height:1.4"><tbody><tr><td style="padding:0px 0px 12px"><img src="https://ci3.googleusercontent.com/meips/ADKq_NYQ2JyWw4de4nk2IAgm_AJdcgvRKAP04KzuLYwBIdvrQvqL7SMUfmGubd_duRT85QgM0v9L-BHtatzMNuJIOzetDUbuiLJZYUsIDOdsNDoUh60BXX_pHifpp4NCi_qFUWr2ra0ZHEpL2EhcFK0sZjnaR4Phmouz-7FJMA=s0-d-e1-ft#https://www.hellolanding.com/blog/wp-content/uploads/2025/08/landing_logomark_landing-bright-blue.png" alt="Landing" width="26" height="28" style="display:block;border:0px;outline:none"></td></tr><tr><td style="padding:0px 0px 8px"><span style="font-size:20px;letter-spacing:0.25px">Landing Member Support</span></td></tr><tr><td style="padding:0px 0px 3px"><span style="font-size:13px;letter-spacing:0.2px;color:rgb(11,56,98);display:block"></span></td></tr><tr><td style="padding:0px 0px 3px"><span style="font-size:13px;letter-spacing:0.2px;color:rgb(11,56,98);display:block">Member Support Team</span><span style="font-size:13px;letter-spacing:0.2px;color:rgb(11,56,98);display:block"></span></td></tr><tr><td><a href="tel:' + LANDING_IVR_PHONE + '" style="color:rgb(17,85,204);font-family:Arial;font-size:9pt" target="_blank">' + LANDING_IVR_PHONE_DISPLAY + '</a><br><a href="http://hellolanding.com" style="color:rgb(11,56,98);font-size:13px;letter-spacing:0.2px;display:inline-block" target="_blank">member.support@hellolanding.com</a></td></tr></tbody></table></div>';

  // Clear run-specific fields
  [
    'window_start', 'window_end', 'manager_email','manager_name', 'property_name', 'event_name',
    'reply_to', 'cc_extra', 'bcc_extra', 'body_full_html', 'attachment_file_ids',
    'test_email', 'notification_card',
  ].forEach(k => setConfigValue_(k, ''));

  // Restore safe defaults
  setConfigValue_('body_intro_html',       genericIntro);
  setConfigValue_('send_mode',            'INDIVIDUAL');
  setConfigValue_('include_disclaimer',    'YES');
  setConfigValue_('closing_html',          genericClosing);
  setConfigValue_('include_unit_line',     'NO');
  setConfigValue_('subject_template',      genericSubject);
  setConfigValue_('sender_display_name',   'Landing Notifications');
  setConfigValue_('disclaimer_html',       genericDisclaimer);
  setConfigValue_('signature_html',        defaultSignature);

  safeAlert_('Full reset complete.');
}

// ── Undo ──────────────────────────────────────────────────────────────────────

/**
 * Reverts Status and Last Sent for every row affected by the last completed run.
 * Emails already sent cannot be recalled — only sheet state is reverted.
 */
function undoLastRunFromLog() {
  const log     = getRunLogSheet_();
  const lastRow = log.getLastRow();
  if (lastRow < 2) return safeAlert_('Run_Log is empty.');

  for (let r = lastRow; r >= 2; r--) {
    const [ts, runId, mode, shName, count, actor, subj, cfgSnap, rowStatesJson, notes, completed] =
      log.getRange(r, 1, 1, 11).getValues()[0];

    if (!rowStatesJson) continue;
    if (String(completed).toUpperCase() !== 'TRUE') continue;

    try {
      const states = JSON.parse(rowStatesJson);
      const sh     = SpreadsheetApp.getActive().getSheetByName(shName);
      if (!sh) return safeAlert_(`Recipients sheet "${shName}" no longer exists.`);

      let reverted = 0;
      states.forEach(s => {
        if (!s || !s.row) return;
        if (s.prevStatus === undefined && s.prevLastSent === undefined) return;

        (s.prevStatus == null)
          ? sh.getRange(s.row, COL.STATUS).clearContent()
          : sh.getRange(s.row, COL.STATUS).setValue(s.prevStatus);

        (!s.prevLastSent)
          ? sh.getRange(s.row, COL.LAST_SENT).clearContent()
          : sh.getRange(s.row, COL.LAST_SENT).setValue(new Date(s.prevLastSent));

        reverted++;
      });

      appendRunLogRow_({
        timestamp:   new Date(),
        runId:       `UNDO-${runId}`,
        mode:        'UNDO',
        shName,
        count:       reverted,
        actor:       Session.getActiveUser().getEmail(),
        subject:     `Undo of ${mode}`,
        cfgSnapshot: '',
        rowStates:   [],
        notes:       `Reverted ${reverted} row(s) from run ${runId}`,
        completed:   true,
      });

      return safeAlert_(`Undo complete: reverted ${reverted} row(s) from run ${runId}.`);
    } catch (e) {
      return safeAlert_('Could not parse previous RowStates JSON: ' + e.message);
    }
  }

  safeAlert_('No completed runs with row state data found to undo.');
}

// ── Signature management ──────────────────────────────────────────────────────

function syncSignatureFromGmailToConfig() {
  try {
    const list   = Gmail.Users.Settings.SendAs.list('me');
    const sendAs = (list && list.sendAs) || [];
    if (!sendAs.length) {
      return safeAlert_('Could not read Gmail signature. Is the Gmail API advanced service enabled?');
    }
    const primary = sendAs.find(s => s.isDefault) || sendAs[0];
    setConfigValue_('signature_html', primary.signature || '');
    safeAlert_(`Signature synced from ${primary.sendAsEmail}.`);
  } catch (e) {
    safeAlert_('Error syncing signature: ' + e.message);
  }
}

function clearSignatureInConfig() {
  setConfigValue_('signature_html', '');
  safeAlert_('signature_html cleared.');
}
