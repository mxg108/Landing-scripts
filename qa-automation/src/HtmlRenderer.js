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
   * @param {string=}         disclaimer — ScorecardActionsDesign §4.2.7/
   *                          §4.3a cause tag sent by the backend when this
   *                          email supersedes or resolves a previous score
   *                          (rescore_manual | rescore_auto | override |
   *                          review_resolution | edit_finalized). Empty/
   *                          unknown → no banner (first-pass emails are
   *                          byte-identical to before).
   */
  constructor(entry, scoreCard, feedbackCard, progressionCard, disclaimer) {
    this.entry           = entry;
    this.scoreCard       = scoreCard;
    this.feedbackCard    = feedbackCard;
    this.progressionCard = progressionCard;
    this.disclaimer      = disclaimer || '';
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

    // ── Disclaimer banner (ScorecardActions S7) ───────────────────
    html += this._renderDisclaimer();

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
  /**
   * The §4.2.7/§4.3a disclaimer banner — rendered only when the backend
   * tagged the dispatch with a cause. The copy names WHY this email
   * supersedes/resolves a previous score; unknown tags get the generic
   * superseded text (forward-compatible with new causes).
   * @private
   */
  _renderDisclaimer() {
    if (!this.disclaimer) return '';
    var TEXTS = {
      rescore_manual:
        'This call was re-evaluated at your team’s request. This email '
        + 'replaces any previous evaluation you received for this call.',
      rescore_auto:
        'As part of routine quality control, the scoring system '
        + 'automatically re-evaluated this call once. This email replaces '
        + 'any previous evaluation you received for this call.',
      override:
        'The overall score in this evaluation was set by a human reviewer '
        + 'and supersedes the automated score. Your team lead or manager '
        + 'will follow up with you about this change.',
      review_resolution:
        'This evaluation was flagged for human review and has been '
        + 'reviewed and finalized by a person before sending.',
      edit_finalized:
        'This evaluation was manually revised by a human reviewer after '
        + 'its original delivery and re-finalized. This email replaces the '
        + 'previous evaluation you received for this call.',
    };
    var text = TEXTS[this.disclaimer]
      || ('This evaluation was updated after its original processing. '
          + 'This email replaces any previous version you received for '
          + 'this call.');
    return '<tr><td style="'
         + 'background:#FFF7ED;'
         + 'border-bottom:1px solid #FDBA74;'
         + 'padding:12px 24px;'
         + 'font-family:Arial,sans-serif;font-size:12px;line-height:1.5;'
         + 'color:#7C2D12;">'
         + '<strong>Please note:</strong> ' + text
         + '</td></tr>';
  }

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
         // "Call · <date>" — the date is the call's connected time
         // (col C, populated by the call-time initiative on the Python
         // side). Explicit label so the manager doesn't read it as
         // "the day this email arrived" or "the day scoring happened."
         + '<td style="font-size:12px;color:' + CONFIG.COLORS.LIGHT_BLUE + ';">'
         + '&#128197; Call &middot; ' + this.entry.formattedDate + '</td>'
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
