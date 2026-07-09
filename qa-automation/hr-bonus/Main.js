/**
 * HR Bonus Sheet — entry points (references/HRBonusSheet.md §6)
 *
 * Standalone (not container-bound) script. The monthly trigger fires in
 * the script timezone (America/Los_Angeles per appsscript.json) on the
 * 1st and exports the month that just ended. Re-runs are idempotent —
 * tabs are cleared and rewritten in place — so any failure is recovered
 * by simply running the export again.
 */

/**
 * Trigger entry point: export the month that just ended.
 * Installed by installMonthlyTrigger().
 */
function runMonthlyExport() {
  exportMonth_(priorMonth_(new Date()));
}

/**
 * Manual re-run for an arbitrary month: set the Script Property
 * MANUAL_MONTH to "YYYY-MM" and run this function from the editor.
 * (Standalone scripts have no spreadsheet menu to hang a prompt on.)
 */
function runManualExport() {
  var month = PropertiesService.getScriptProperties().getProperty("MANUAL_MONTH");
  if (!month || !/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
    throw new Error('Set Script Property MANUAL_MONTH to "YYYY-MM" before running.');
  }
  exportMonth_(month);
}

/**
 * One-time setup: install the monthly trigger (1st of each month,
 * 06:00–07:00 script time). Deletes any prior runMonthlyExport triggers
 * first so repeated runs never stack duplicates.
 */
function installMonthlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === "runMonthlyExport") {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger("runMonthlyExport")
    .timeBased()
    .onMonthDay(1)
    .atHour(6)
    .create();
  Logger.log("Monthly trigger installed: runMonthlyExport, 1st of month ~06:00 %s",
             Session.getScriptTimeZone());
}

// ---------------------------------------------------------------------------

/**
 * Fetch the month payload from the backend and render the workbook.
 * On any error: email the operator, then rethrow so the failure is
 * visible in the Apps Script executions log.
 */
function exportMonth_(month) {
  try {
    var payload = fetchMonthPayload_(month);
    var counts = writeMonthToWorkbook_(payload);
    Logger.log("Exported %s: %s agents, %s evaluations",
               month, counts.agents, counts.evaluations);
  } catch (e) {
    notifyFailure_(month, e);
    throw e;
  }
}

/** "YYYY-MM" of the month before the given date, in the script timezone. */
function priorMonth_(now) {
  var tz = Session.getScriptTimeZone();
  var year = Number(Utilities.formatDate(now, tz, "yyyy"));
  var month = Number(Utilities.formatDate(now, tz, "MM"));
  if (month === 1) {
    return (year - 1) + "-12";
  }
  return year + "-" + (month - 1 < 10 ? "0" : "") + (month - 1);
}

/** Email the operator about a failed export. ALERT_EMAIL Script Property
 *  overrides the default (the account the trigger runs as). */
function notifyFailure_(month, error) {
  var to = PropertiesService.getScriptProperties().getProperty("ALERT_EMAIL")
    || Session.getEffectiveUser().getEmail();
  MailApp.sendEmail({
    to: to,
    subject: "[HR Bonus Sheet] Export FAILED for " + month,
    body: "The HR bonus export for " + month + " failed.\n\n" +
          "Error: " + error + "\n\n" +
          "Re-run is safe (tabs rewrite in place): fix the cause, then run " +
          "runManualExport with Script Property MANUAL_MONTH=" + month + ".",
  });
}
