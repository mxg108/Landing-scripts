/**
 * HR Bonus Sheet — workbook renderer (references/HRBonusSheet.md §2)
 *
 * Renders the backend month payload verbatim: one summary tab per month
 * (inserted at index 0 when new) + one detail tab per agent. Existing
 * tabs are cleared and rewritten in place — never deleted — so re-runs
 * are idempotent and external links to tabs survive.
 */

/**
 * Write the whole month: summary tab + one detail tab per agent.
 * @param {Object} payload the §5 backend payload
 * @return {{agents: number, evaluations: number}} counts for logging
 */
function writeMonthToWorkbook_(payload) {
  var props = PropertiesService.getScriptProperties();
  var workbook = SpreadsheetApp.openById(
    requiredProperty_(props, "HR_BONUS_SPREADSHEET_ID"));

  writeSummaryTab_(workbook, payload);
  var evaluations = 0;
  payload.agents.forEach(function (agent) {
    writeDetailTab_(workbook, payload, agent);
    evaluations += agent.evaluations.length;
  });
  return { agents: payload.agents.length, evaluations: evaluations };
}

function writeSummaryTab_(workbook, payload) {
  var header = ["Agent Name", "Agent Email", "Monthly Avg"]
    .concat(payload.section_labels);
  var rows = payload.agents.map(function (agent) {
    return [agent.name, agent.email, agent.monthly_avg]
      .concat(agent.section_summaries);
  });
  renderTab_(workbook, payload.month_label, [header].concat(rows), 0);
}

function writeDetailTab_(workbook, payload, agent) {
  var header = ["Period", "Date", "Overall Score"]
    .concat(payload.section_labels)
    .concat(["Evaluator", "Dialpad Link"]);
  var rows = agent.evaluations.map(function (e) {
    return [e.period, e.date, e.overall_score]
      .concat(e.sections)
      .concat([e.evaluator, e.dialpad_link]);
  });
  renderTab_(workbook, agent.name, [header].concat(rows), null);
}

/**
 * Clear-in-place render: reuse the tab when it exists (preserving its id
 * and external links), otherwise insert it — at `index` when given.
 * One setValues call per tab; SpreadsheetApp batches internally, so the
 * Sheets-API write-quota pacing the mockup needed doesn't apply here.
 */
function renderTab_(workbook, title, grid, index) {
  var sheet = workbook.getSheetByName(title);
  if (!sheet) {
    sheet = index === null
      ? workbook.insertSheet(title)
      : workbook.insertSheet(title, index);
  }
  sheet.clear();
  sheet.getRange(1, 1, grid.length, grid[0].length).setValues(grid);
  sheet.setFrozenRows(1);
}
