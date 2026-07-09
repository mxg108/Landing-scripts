/**
 * HR Bonus Sheet — backend API client (references/HRBonusSheet.md §5)
 *
 * One GET per export: /api/{team_id}/hr-bonus/{month} with the team's
 * API key. The response is consumed 1:1 by SheetWriter — no math, no
 * reshaping happens on this side.
 */

/**
 * Fetch the month payload from the backend.
 * @param {string} month "YYYY-MM"
 * @return {Object} the §5 payload
 */
function fetchMonthPayload_(month) {
  var props = PropertiesService.getScriptProperties();
  var base = requiredProperty_(props, "BACKEND_BASE_URL").replace(/\/+$/, "");
  var key = requiredProperty_(props, "HR_BONUS_API_KEY");

  var url = base + "/api/" + encodeURIComponent(CONFIG.TEAM_ID)
    + "/hr-bonus/" + encodeURIComponent(month);
  var response = UrlFetchApp.fetch(url, {
    method: "get",
    headers: { Authorization: "Bearer " + key },
    muteHttpExceptions: true,
  });

  var code = response.getResponseCode();
  if (code !== 200) {
    throw new Error("Backend returned " + code + " for " + url + ": "
      + response.getContentText().slice(0, 500));
  }
  return JSON.parse(response.getContentText());
}

function requiredProperty_(props, name) {
  var value = props.getProperty(name);
  if (!value) {
    throw new Error("Script Property " + name + " is not set — see hr-bonus/README.md.");
  }
  return value;
}
