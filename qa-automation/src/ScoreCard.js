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
      var reasoning  = (this.entry.aiReasoning  && this.entry.aiReasoning[cat.key])  || '';
      var confidence = (this.entry.aiConfidence && this.entry.aiConfidence[cat.key]) || '';
      var hasReasoning = !!reasoning;

      // Bottom border lives on whichever row is the LAST visual row for this category
      var scoreRowBorder = (isLast || hasReasoning) ? '' : 'border-bottom:1px solid #F0F0F0;';

      html += '<tr>'
            // Label + confidence chip
            + '<td style="padding:6px 8px 6px 0;font-size:13px;color:' + CONFIG.COLORS.TEXT_GRAY
            + ';width:45%;' + scoreRowBorder + '">'
            + this._esc(cat.label) + this._renderConfidenceChip(confidence) + '</td>'
            // Bar
            + '<td style="padding:6px 8px;width:40%;' + scoreRowBorder + '">'
            + this._renderBar(pct, color)
            + '</td>'
            // Value
            + '<td style="padding:6px 0 6px 8px;font-size:14px;font-weight:bold;color:' + color
            + ';text-align:right;width:15%;' + scoreRowBorder + '">'
            + score.toFixed(1) + '/5</td>'
            + '</tr>';

      if (hasReasoning) {
        var reasoningBorder = isLast ? '' : 'border-bottom:1px solid #F0F0F0;';
        html += '<tr><td colspan="3" style="padding:0 0 8px 0;font-size:12px;color:'
              + CONFIG.COLORS.TEXT_GRAY + ';font-style:italic;line-height:1.4;'
              + reasoningBorder + '">' + this._esc(reasoning) + '</td></tr>';
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
      var bReasoning  = (this.entry.aiReasoning  && this.entry.aiReasoning[bcat.key])  || '';
      var bConfidence = (this.entry.aiConfidence && this.entry.aiConfidence[bcat.key]) || '';

      html += '<tr>'
            + '<td style="padding:6px 8px 6px 0;font-size:13px;color:' + CONFIG.COLORS.TEXT_GRAY + ';">'
            + this._esc(bcat.label) + this._renderConfidenceChip(bConfidence) + '</td>'
            + '<td style="padding:6px 0;text-align:right;font-size:14px;font-weight:bold;color:'
            + (passed ? CONFIG.COLORS.GREEN : CONFIG.COLORS.RED) + ';">'
            + (passed ? '&#10003; Yes' : '&#10007; No') + '</td>'
            + '</tr>';

      if (bReasoning) {
        html += '<tr><td colspan="2" style="padding:0 0 8px 0;font-size:12px;color:'
              + CONFIG.COLORS.TEXT_GRAY + ';font-style:italic;line-height:1.4;">'
              + this._esc(bReasoning) + '</td></tr>';
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
          + overall.toFixed(1) + '</span>'
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
   * Renders a small bordered pill showing AI confidence ("high" / "medium" /
   * "low" / "manual"). Returns an empty string for empty / unrecognized values.
   * @private
   * @param  {string} confidence
   * @return {string}
   */
  _renderConfidenceChip(confidence) {
    var c = (confidence || '').toString().toLowerCase().trim();
    if (!c) return '';

    var color;
    switch (c) {
      case 'high':   color = CONFIG.COLORS.GREEN;     break;
      case 'medium': color = CONFIG.COLORS.AMBER;     break;
      case 'low':    color = CONFIG.COLORS.RED;       break;
      case 'manual': color = CONFIG.COLORS.TEXT_GRAY; break;
      default: return '';
    }
    return ' <span style="display:inline-block;border:1px solid ' + color
         + ';color:' + color + ';font-size:9px;padding:1px 6px;border-radius:8px;'
         + 'text-transform:uppercase;letter-spacing:0.5px;font-weight:bold;'
         + 'vertical-align:middle;margin-left:4px;">' + this._esc(c) + '</span>';
  }

  /** @private — HTML-escape a string. */
  _esc(str) {
    return (str || '').toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}
