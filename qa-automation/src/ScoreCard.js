/**
 * QA Automation — ScoreCard
 *
 * Renders an HTML card displaying:
 *   • Numeric scores (1-5) as color-coded rows with visual bars
 *   • Binary checks (Y/N) as checkmark / X badges
 *   • Overall score in the footer
 */

class ScoreCard {
  /**
   * @param {QAEntry} entry
   */
  constructor(entry) {
    this.entry = entry;
  }

  /**
   * Returns a fully-styled HTML string for the score card.
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
          + 'Score Breakdown</td></tr>';

    // ── Numeric scores ────────────────────────────────────────────
    html += '<tr><td style="padding:16px;font-family:Arial,sans-serif;">';
    html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">';

    var categories = CONFIG.NUMERIC_CATEGORIES;
    for (var i = 0; i < categories.length; i++) {
      var cat   = categories[i];
      var score = this.entry.numericScores[cat.key];
      var color = QAEntry.colorForScore(score);
      var pct   = (score / 5 * 100).toFixed(0);
      var isLast = i === categories.length - 1;
      var reasoning = (this.entry.aiReasoning && this.entry.aiReasoning[cat.key]) || '';
      var hasReasoning = !!reasoning;

      // Score row gets a bottom separator only when this is the FINAL visual
      // row for the section (no reasoning box below) AND it isn't the very
      // last section in the card.
      var scoreRowBorder = (isLast || hasReasoning) ? '' : 'border-bottom:1px solid #F0F0F0;';

      html += '<tr>'
            + '<td style="padding:6px 8px 6px 0;font-size:13px;color:' + CONFIG.COLORS.TEXT_GRAY
            + ';width:45%;' + scoreRowBorder + '">'
            + this._esc(cat.label) + '</td>'
            + '<td style="padding:6px 8px;width:40%;' + scoreRowBorder + '">'
            + this._renderBar(pct, color)
            + '</td>'
            + '<td style="padding:6px 0 6px 8px;font-size:14px;font-weight:bold;color:' + color
            + ';text-align:right;width:15%;' + scoreRowBorder + '">'
            + this._formatScore(score) + '/5</td>'
            + '</tr>';

      if (hasReasoning) {
        html += '<tr><td colspan="3" style="padding:2px 0 ' + (isLast ? '0' : '12px') + ' 0;">'
              + this._renderReasoningBox(reasoning)
              + '</td></tr>';
      }
    }

    html += '</table></td></tr>';

    // ── Binary checks ─────────────────────────────────────────────
    html += '<tr><td style="padding:0 16px 12px 16px;font-family:Arial,sans-serif;">';
    html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
          + ' style="border-top:2px solid ' + CONFIG.COLORS.LIGHT_BLUE + ';padding-top:8px;">';

    var binaries = CONFIG.BINARY_CATEGORIES;
    for (var j = 0; j < binaries.length; j++) {
      var bcat   = binaries[j];
      var passed = this.entry.binaryChecks[bcat.key];
      var bReasoning = (this.entry.aiReasoning && this.entry.aiReasoning[bcat.key]) || '';

      html += '<tr>'
            + '<td style="padding:6px 8px 6px 0;font-size:13px;color:' + CONFIG.COLORS.TEXT_GRAY + ';">'
            + this._esc(bcat.label) + '</td>'
            + '<td style="padding:6px 0;text-align:right;font-size:14px;font-weight:bold;color:'
            + (passed ? CONFIG.COLORS.GREEN : CONFIG.COLORS.RED) + ';">'
            + (passed ? '&#10003; Yes' : '&#10007; No') + '</td>'
            + '</tr>';

      if (bReasoning) {
        html += '<tr><td colspan="2" style="padding:2px 0 8px 0;">'
              + this._renderReasoningBox(bReasoning)
              + '</td></tr>';
      }
    }

    html += '</table></td></tr>';

    // ── Overall score footer ──────────────────────────────────────
    var overall      = this.entry.overallScore;
    var overallColor = QAEntry.colorForOverallScore(overall);

    html += '<tr><td style="background:' + CONFIG.COLORS.LIGHT_BLUE
          + ';padding:12px 16px;text-align:center;font-family:Arial,sans-serif;">'
          + '<span style="font-size:13px;color:' + CONFIG.COLORS.TEXT_GRAY + ';">Overall Score</span>'
          + '<br>'
          + '<span style="font-size:28px;font-weight:bold;color:' + overallColor + ';">'
          + this._formatScore(overall) + '</span>'
          + '</td></tr>';

    html += '</table>';
    return html;
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /**
   * Renders a simple horizontal bar using nested tables (for email compatibility).
   * @private
   * @param {number} pct  — 0-100
   * @param {string} color — hex
   * @return {string}
   */
  _renderBar(pct, color) {
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
         + 'style="border-radius:4px;overflow:hidden;">'
         + '<tr>'
         + '<td style="background:' + color + ';height:8px;width:' + pct + '%;border-radius:4px 0 0 4px;"></td>'
         + '<td style="background:' + CONFIG.COLORS.LIGHT_BLUE + ';height:8px;width:' + (100 - pct) + '%;border-radius:0 4px 4px 0;"></td>'
         + '</tr></table>';
  }

  /**
   * Renders the AI reasoning text wrapped in a subtle bordered box.
   * @private
   * @param  {string} reasoning
   * @return {string}
   */
  _renderReasoningBox(reasoning) {
    return '<div style="border:1px solid #EEEEEE;border-radius:4px;'
         + 'background:#FAFAFA;padding:8px 12px;font-size:12px;color:'
         + CONFIG.COLORS.TEXT_GRAY + ';font-style:italic;line-height:1.5;">'
         + this._esc(reasoning) + '</div>';
  }

  /**
   * Formats a 1–5 score for display. Drops the trailing ".0" when the score
   * is a whole number ("5" instead of "5.0") but preserves one decimal for
   * fractional scores ("4.5").
   * @private
   * @param  {number} score
   * @return {string}
   */
  _formatScore(score) {
    return score % 1 === 0 ? String(score) : score.toFixed(1);
  }

  /** @private — HTML-escape a string. */
  _esc(str) {
    return (str || '').toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}
