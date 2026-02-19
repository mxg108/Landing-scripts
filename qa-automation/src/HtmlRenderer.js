/**
 * QA Automation — HtmlRenderer
 *
 * Orchestrates the full email layout by composing the header,
 * card slots (ScoreCard, FeedbackCard, ProgressionCard), and footer
 * into a single Gmail-compatible HTML string.
 *
 * All styles are inline — Gmail strips <style> blocks.
 */

class HtmlRenderer {
  /**
   * @param {QAEntry}         entry
   * @param {ScoreCard}       scoreCard
   * @param {FeedbackCard}    feedbackCard
   * @param {ProgressionCard} progressionCard
   */
  constructor(entry, scoreCard, feedbackCard, progressionCard) {
    this.entry           = entry;
    this.scoreCard       = scoreCard;
    this.feedbackCard    = feedbackCard;
    this.progressionCard = progressionCard;
  }

  /**
   * Renders the complete email HTML.
   * @return {string}
   */
  renderEmail() {
    var html = '';

    // ── Outer wrapper (centers content, sets max-width) ───────────
    html += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
          + 'style="background:#F5F5F5;padding:24px 0;">'
          + '<tr><td align="center">'
          + '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
          + 'style="background:' + CONFIG.COLORS.WHITE + ';border-radius:8px;'
          + 'overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">';

    // ── Email header ──────────────────────────────────────────────
    html += this._renderHeader();

    // ── Card slots ────────────────────────────────────────────────
    html += '<tr><td style="padding:24px;">';
    html += this.scoreCard.render();
    html += this.feedbackCard.render();
    html += this.progressionCard.render();
    html += '</td></tr>';

    // ── Footer ────────────────────────────────────────────────────
    html += this._renderFooter();

    // ── Close wrappers ────────────────────────────────────────────
    html += '</table></td></tr></table>';

    return html;
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /** @private */
  _renderHeader() {
    return '<tr><td style="'
         + 'background:' + CONFIG.COLORS.DARK_NAVY + ';'
         + 'padding:24px 24px 20px 24px;'
         + 'font-family:Arial,sans-serif;'
         + '">'
         // Title
         + '<div style="font-size:22px;font-weight:bold;color:' + CONFIG.COLORS.WHITE + ';'
         + 'margin-bottom:8px;">QA Evaluation</div>'
         // Agent name
         + '<div style="font-size:16px;color:' + CONFIG.COLORS.LIGHT_BLUE + ';'
         + 'margin-bottom:12px;">' + this._esc(this.entry.agentName) + '</div>'
         // Meta row
         + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
         + '<tr>'
         + '<td style="font-size:12px;color:' + CONFIG.COLORS.LIGHT_BLUE + ';">'
         + '&#128197; ' + this.entry.formattedDate + '</td>'
         + '<td style="text-align:right;">'
         + '<span style="font-size:12px;color:' + CONFIG.COLORS.WHITE + ';'
         + 'border:1px solid ' + CONFIG.COLORS.LIGHT_BLUE + ';'
         + 'border-radius:4px;padding:3px 8px;'
         + '">&#128100; ' + this._esc(QAEntry.managerNameFromEmail(this.entry.managerEmail)) + '</span></td>'
         + '</tr></table>'
         + '</td></tr>';
  }

  /** @private */
  _renderFooter() {
    return '<tr><td style="'
         + 'background:' + CONFIG.COLORS.LIGHT_BLUE + ';'
         + 'padding:16px 24px;'
         + 'text-align:center;'
         + 'font-family:Arial,sans-serif;'
         + 'font-size:11px;'
         + 'color:' + CONFIG.COLORS.TEXT_GRAY + ';'
         + '">'
         + 'Landing QA System &middot; This is an automated evaluation report'
         + '</td></tr>';
  }

  /** @private */
  _esc(str) {
    return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}
