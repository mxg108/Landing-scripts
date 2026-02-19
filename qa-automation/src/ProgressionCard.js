/**
 * QA Automation — ProgressionCard
 *
 * Renders an HTML card showing the analyst's QA history:
 *   • Compact table of the last N evaluations
 *   • Overall score with delta arrows (▲ ▼ ●)
 *   • Inline "spark bars" for visual comparison
 */

class ProgressionCard {
  /**
   * @param {QAEntry}   currentEntry  — the QA that was just submitted
   * @param {Object[]}  history       — past entries from AnalystHistory.getHistory()
   *                                     (newest-first, may include current if already appended)
   */
  constructor(currentEntry, history) {
    this.current = currentEntry;
    this.history = history;
  }

  /**
   * Returns a fully-styled HTML string for the progression card.
   * @return {string}
   */
  render() {
    var html = '';

    // ── Card container ────────────────────────────────────────────
    html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="'
          + 'border:1px solid #E0E0E0;border-radius:8px;overflow:hidden;margin-bottom:16px;">';

    // ── Header ────────────────────────────────────────────────────
    html += '<tr><td style="background:' + CONFIG.COLORS.DARK_NAVY + ';color:' + CONFIG.COLORS.WHITE
          + ';padding:12px 16px;font-size:16px;font-weight:bold;font-family:Arial,sans-serif;">'
          + 'QA Progression</td></tr>';

    // ── Body ──────────────────────────────────────────────────────
    html += '<tr><td style="padding:16px;font-family:Arial,sans-serif;">';

    if (this.history.length <= 1) {
      var firstMsg = CONFIG.FIRST_EVAL_MESSAGE.replace('{{agentName}}', this._esc(this.current.agentName));
      var firstStyle = CONFIG.FIRST_EVAL_STYLE.replace('{{textGray}}', CONFIG.COLORS.TEXT_GRAY);
      html += '<p style="' + firstStyle + '">' + firstMsg + '</p>';
    }

    if (this.history.length > 0) {
      html += this._renderHistoryTable();
    }

    // Achievement card (shown regardless of history)
    html += this._renderAchievementCard();

    html += '</td></tr>';
    html += '</table>';
    return html;
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /** @private — builds the history comparison table. */
  _renderHistoryTable() {
    var html = '';
    html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
          + 'style="font-size:13px;">';

    // Header row
    html += '<tr style="border-bottom:2px solid ' + CONFIG.COLORS.LIGHT_BLUE + ';">'
          + '<th style="text-align:left;padding:6px 8px;color:' + CONFIG.COLORS.TEXT_GRAY
          + ';font-weight:bold;font-size:11px;text-transform:uppercase;">Date</th>'
          + '<th style="text-align:center;padding:6px 8px;color:' + CONFIG.COLORS.TEXT_GRAY
          + ';font-weight:bold;font-size:11px;text-transform:uppercase;">Score</th>'
          + '<th style="text-align:center;padding:6px 8px;color:' + CONFIG.COLORS.TEXT_GRAY
          + ';font-weight:bold;font-size:11px;text-transform:uppercase;">Trend</th>'
          + '<th style="text-align:left;padding:6px 8px;color:' + CONFIG.COLORS.TEXT_GRAY
          + ';font-weight:bold;font-size:11px;text-transform:uppercase;width:40%;">Progress</th>'
          + '</tr>';

    // Build rows: oldest-first for chronological reading, then reverse back
    var entries = this.history.slice().reverse(); // oldest first

    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i];
      var prev  = i > 0 ? entries[i - 1] : null;
      var score = entry.overallScore;
      var color = QAEntry.colorForOverallScore(score);
      var delta = this._deltaSymbol(score, prev ? prev.overallScore : null);
      var pct   = Math.min(score, 100).toFixed(0);
      var dateStr = this._formatDate(entry.timestamp);
      var isCurrent = (i === entries.length - 1);
      var isFirst   = (i === 0);
      var rowBg = isCurrent ? CONFIG.COLORS.LIGHT_BLUE : CONFIG.COLORS.WHITE;

      html += '<tr style="background:' + rowBg + ';'
            + (i < entries.length - 1 ? 'border-bottom:1px solid #F0F0F0;' : '') + '">'
            + '<td style="padding:8px;color:' + CONFIG.COLORS.DARK_NAVY + ';'
            + (isCurrent ? 'font-weight:bold;' : '') + '">'
            + dateStr + (isCurrent ? ' &#9668;' : '') + '</td>'
            + '<td style="padding:8px;text-align:center;font-weight:bold;color:' + color + ';">'
            + score.toFixed(1) + '</td>'
            + '<td style="padding:8px;text-align:center;font-size:16px;">'
            + delta + '</td>'
            + '<td style="padding:8px;">' + this._renderMiniBar(pct, color, isFirst) + '</td>'
            + '</tr>';
    }

    html += '</table>';
    return html;
  }

  /**
   * Returns a colored delta arrow comparing current to previous score.
   * @private
   * @param {number}      current
   * @param {number|null} previous
   * @return {string}
   */
  _deltaSymbol(current, previous) {
    if (previous === null) return '<span style="color:' + CONFIG.COLORS.TEXT_GRAY + ';">&#8212;</span>';
    var diff = current - previous;
    if (diff > 0.5)  return '<span style="color:' + CONFIG.COLORS.GREEN + ';">&#9650;</span>';
    if (diff < -0.5) return '<span style="color:' + CONFIG.COLORS.RED   + ';">&#9660;</span>';
    return '<span style="color:' + CONFIG.COLORS.AMBER + ';">&#9679;</span>';
  }

  /**
   * Renders a compact inline bar for the progression table.
   * The bar represents the full 0-100 score range with a goal marker at QA_GOAL (85).
   * Only a score of 100 fills the bar completely (golden).
   * @private
   * @param {number}  pct    — fill percentage (0-100, mapped 1:1 from score)
   * @param {string}  color
   * @param {boolean} showGoal — if true, adds an "85" label above the bar
   * @return {string}
   */
  _renderMiniBar(pct, color, showGoal) {
    var html = '';
    var score = parseFloat(pct);
    var goalPct = CONFIG.QA_GOAL;

    if (showGoal) {
      html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + '<tr>'
            + '<td style="width:' + goalPct + '%;text-align:right;font-size:9px;color:'
            + CONFIG.COLORS.TEXT_GRAY + ';padding:0 0 1px 0;font-family:Arial,sans-serif;line-height:1;">'
            + goalPct + '</td>'
            + '<td style="width:' + (100 - goalPct) + '%;"></td>'
            + '</tr></table>';
    }

    html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
         + 'style="border-radius:3px;overflow:hidden;">'
         + '<tr>';

    if (score >= goalPct) {
      // Fill extends to or past the goal — show marker line at goal boundary
      html += '<td style="background:' + color + ';height:6px;width:' + goalPct
            + '%;border-right:2px solid ' + CONFIG.COLORS.WHITE + ';"></td>';
      var pastGoal = Math.min(score, 100) - goalPct;
      if (pastGoal > 0) {
        html += '<td style="background:' + color + ';height:6px;width:' + pastGoal + '%;"></td>';
      }
      var empty = 100 - Math.min(score, 100);
      if (empty > 0) {
        html += '<td style="background:' + CONFIG.COLORS.LIGHT_BLUE + ';height:6px;width:' + empty + '%;"></td>';
      }
    } else {
      // Fill below goal — subtle tick at goal position on the empty track
      html += '<td style="background:' + color + ';height:6px;width:' + score + '%;"></td>';
      html += '<td style="background:' + CONFIG.COLORS.LIGHT_BLUE + ';height:6px;width:' + (goalPct - score)
            + '%;border-right:2px solid #D0D0D0;"></td>';
      html += '<td style="background:' + CONFIG.COLORS.LIGHT_BLUE + ';height:6px;width:' + (100 - goalPct) + '%;"></td>';
    }

    html += '</tr></table>';
    return html;
  }

  /**
   * Renders a small achievement card based on the current entry's overall score.
   * @private
   * @return {string}
   */
  _renderAchievementCard() {
    var score = this.current.overallScore;
    if (score >= 100) {
      return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
           + 'style="margin-top:12px;border-radius:6px;overflow:hidden;">'
           + '<tr><td style="'
           + 'background:' + CONFIG.COLORS.GOLD_LIGHT + ';'
           + 'border-left:4px solid ' + CONFIG.COLORS.GOLD + ';'
           + 'padding:10px 14px;'
           + 'font-family:Arial,sans-serif;'
           + 'font-size:14px;'
           + 'font-weight:bold;'
           + 'color:' + CONFIG.COLORS.GOLD_DARK + ';'
           + 'text-align:center;'
           + '">&#9733; Perfect call!</td></tr></table>';
    } else if (score >= CONFIG.QA_GOAL) {
      return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
           + 'style="margin-top:12px;border-radius:6px;overflow:hidden;">'
           + '<tr><td style="'
           + 'background:' + CONFIG.COLORS.GREEN_LIGHT + ';'
           + 'border-left:4px solid ' + CONFIG.COLORS.GREEN + ';'
           + 'padding:10px 14px;'
           + 'font-family:Arial,sans-serif;'
           + 'font-size:14px;'
           + 'font-weight:bold;'
           + 'color:' + CONFIG.COLORS.GREEN + ';'
           + 'text-align:center;'
           + '">&#10003; QA goal reached!</td></tr></table>';
    }
    return '';
  }

  /** @private */
  _formatDate(date) {
    return Utilities.formatDate(
      new Date(date),
      Session.getScriptTimeZone(),
      'MMM dd, yyyy'
    );
  }

  /** @private */
  _esc(str) {
    return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}
