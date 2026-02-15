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

    if (this.history.length === 0) {
      html += '<p style="font-size:14px;color:' + CONFIG.COLORS.TEXT_GRAY + ';margin:0;">'
            + 'This is ' + this._esc(this.current.agentName)
            + '\'s first QA evaluation. Future emails will show score trends here.</p>';
    } else {
      html += this._renderHistoryTable();
    }

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
      var color = QAEntry.colorForScore(score);
      var delta = this._deltaSymbol(score, prev ? prev.overallScore : null);
      var pct   = (score / 5 * 100).toFixed(0);
      var dateStr = this._formatDate(entry.timestamp);
      var isCurrent = (i === entries.length - 1);
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
            + '<td style="padding:8px;">' + this._renderMiniBar(pct, color) + '</td>'
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
    if (diff > 0.05)  return '<span style="color:' + CONFIG.COLORS.GREEN + ';">&#9650;</span>';
    if (diff < -0.05) return '<span style="color:' + CONFIG.COLORS.RED   + ';">&#9660;</span>';
    return '<span style="color:' + CONFIG.COLORS.AMBER + ';">&#9679;</span>';
  }

  /**
   * Renders a compact inline bar for the progression table.
   * @private
   * @param {number} pct
   * @param {string} color
   * @return {string}
   */
  _renderMiniBar(pct, color) {
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
         + 'style="border-radius:3px;overflow:hidden;">'
         + '<tr>'
         + '<td style="background:' + color + ';height:6px;width:' + pct + '%;"></td>'
         + '<td style="background:' + CONFIG.COLORS.LIGHT_BLUE + ';height:6px;width:' + (100 - pct) + '%;"></td>'
         + '</tr></table>';
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
