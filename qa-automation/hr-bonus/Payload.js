/**
 * HR Bonus Sheet — payload mode (Sandy-era transport, 2026-09).
 *
 * Railway's suite PULLED /api/{team}/hr-bonus/{month}; Sandy apps sit
 * behind the Google SSO wall, so the direction flips: the Sandy EOM cron
 * (or a manual dispatch) POSTs {hr_bonus: <§5 payload>} here and the
 * existing renderer writes the workbook unchanged. Same flip every other
 * GAS surface got (scorecard payload mode, sofia digest).
 *
 * The pull path (ApiClient.js + the monthly trigger) is retired — delete
 * the runMonthlyExport trigger in the editor; the code stays as a
 * reference until full Railway decommission.
 *
 * GAS cannot set HTTP status codes: receipts ride the JSON body.
 */
function doPost(e) {
  var receipt = function (obj) {
    return ContentService.createTextOutput(JSON.stringify(obj))
      .setMimeType(ContentService.MimeType.JSON);
  };
  var body;
  try {
    body = JSON.parse(e && e.postData ? e.postData.contents : "{}");
  } catch (err) {
    return receipt({ status: "error", message: "invalid JSON body" });
  }
  var payload = body && body.hr_bonus;
  if (!payload || !payload.month || !Array.isArray(payload.agents)) {
    return receipt({ status: "error", message: "expected {hr_bonus: {month, agents, ...}}" });
  }
  try {
    var counts = writeMonthToWorkbook_(payload);
    return receipt({
      status: "written",
      month: payload.month,
      month_label: payload.month_label,
      agents: counts.agents,
      evaluations: counts.evaluations,
    });
  } catch (err) {
    return receipt({ status: "error", message: String(err).slice(0, 300) });
  }
}
