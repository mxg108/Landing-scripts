/**
 * QA Automation — FeedbackCard
 *
 * Renders an HTML card with two styled sections:
 *   • Key Strengths   — blue left-border, light-blue background
 *   • Opportunities   — dark-navy left-border, white background
 *   • Dialpad link    — call recording button at the bottom
 */

class FeedbackCard {
  /**
   * @param {QAEntry} entry
   */
  constructor(entry) {
    this.entry = entry;
  }

  /**
   * Returns a fully-styled HTML string for the feedback card.
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
          + 'Feedback</td></tr>';

    // ── Key Strengths ─────────────────────────────────────────────
    html += '<tr><td style="padding:16px 16px 8px 16px;font-family:Arial,sans-serif;">';
    html += this._renderSection(
      'Key Strengths',
      this.entry.strengths,
      CONFIG.COLORS.ACCENT_BLUE,
      CONFIG.COLORS.LIGHT_BLUE
    );
    html += '</td></tr>';

    // ── Opportunities for Improvement ─────────────────────────────
    html += '<tr><td style="padding:8px 16px 16px 16px;font-family:Arial,sans-serif;">';
    html += this._renderSection(
      'Opportunities for Improvement',
      this.entry.improvements,
      CONFIG.COLORS.DARK_NAVY,
      CONFIG.COLORS.WHITE
    );
    html += '</td></tr>';

    // ── Dialpad call link ─────────────────────────────────────────
    if (this.entry.dialpadLink) {
      html += '<tr><td style="padding:0 16px 16px 16px;font-family:Arial,sans-serif;text-align:center;">';
      html += '<a href="' + this._esc(this.entry.dialpadLink) + '" target="_blank" style="'
            + 'display:inline-block;padding:8px 20px;background:' + CONFIG.COLORS.ACCENT_BLUE
            + ';color:' + CONFIG.COLORS.WHITE + ';text-decoration:none;border-radius:4px;'
            + 'font-size:13px;font-weight:bold;">'
            + '&#9654; Review Call Recording</a>';
      html += '</td></tr>';
    }

    html += '</table>';
    return html;
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /**
   * Renders a left-bordered block with a label and body text.
   * @private
   * @param {string} label
   * @param {string} text
   * @param {string} borderColor
   * @param {string} bgColor
   * @return {string}
   */
  _renderSection(label, text, borderColor, bgColor) {
    var body = text || '<em style="color:#999;">No feedback provided.</em>';
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="'
         + 'border-left:4px solid ' + borderColor + ';background:' + bgColor
         + ';border-radius:0 4px 4px 0;">'
         + '<tr><td style="padding:12px 16px;">'
         + '<div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:'
         + CONFIG.COLORS.TEXT_GRAY + ';margin-bottom:6px;font-weight:bold;">'
         + this._esc(label) + '</div>'
         + '<div style="font-size:14px;color:' + CONFIG.COLORS.DARK_NAVY + ';line-height:1.5;">'
         + this._esc(body) + '</div>'
         + '</td></tr></table>';
  }

  /** @private */
  _esc(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}
