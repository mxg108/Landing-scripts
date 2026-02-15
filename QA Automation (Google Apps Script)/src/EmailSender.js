/**
 * QA Automation — EmailSender
 *
 * Handles email composition and delivery via GmailApp.
 * Sends the rendered HTML evaluation to the agent (To) and manager (CC).
 */

class EmailSender {
  /**
   * @param {QAEntry} entry — the QA entry providing recipient addresses and metadata
   */
  constructor(entry) {
    this.entry = entry;
  }

  /**
   * Sends the QA evaluation email.
   *
   * @param {string} htmlBody — fully rendered HTML from HtmlRenderer.renderEmail()
   */
  send(htmlBody) {
    this._validate();

    var subject = this._buildSubject();

    GmailApp.sendEmail(
      this.entry.agentEmail,      // To: the analyst
      subject,
      this._plainTextFallback(),  // plain-text fallback for clients that don't render HTML
      {
        htmlBody: htmlBody,
        cc:       this.entry.managerEmail,
        name:     'Landing QA System',
      }
    );

    Logger.log(
      'QA email sent → To: %s | CC: %s | Subject: %s',
      this.entry.agentEmail,
      this.entry.managerEmail,
      subject
    );
  }

  /**
   * Creates a Gmail draft instead of sending (useful for review / dry-run).
   *
   * @param {string} htmlBody — fully rendered HTML from HtmlRenderer.renderEmail()
   * @return {GmailDraft}
   */
  createDraft(htmlBody) {
    this._validate();

    var subject = this._buildSubject();

    var draft = GmailApp.createDraft(
      this.entry.agentEmail,
      subject,
      this._plainTextFallback(),
      {
        htmlBody: htmlBody,
        cc:       this.entry.managerEmail,
        name:     'Landing QA System',
      }
    );

    Logger.log('QA draft created → To: %s | Subject: %s', this.entry.agentEmail, subject);
    return draft;
  }

  // ────────────────────────────────────────────────────────────────
  // Private helpers
  // ────────────────────────────────────────────────────────────────

  /** @private — builds the email subject from the template. */
  _buildSubject() {
    return CONFIG.EMAIL.SUBJECT_TEMPLATE
      .replace('{{agentName}}', this.entry.agentName)
      .replace('{{date}}',      this.entry.formattedDate);
  }

  /** @private — basic plain-text fallback for non-HTML email clients. */
  _plainTextFallback() {
    return 'QA Evaluation for ' + this.entry.agentName
         + ' (' + this.entry.formattedDate + ')'
         + '\nOverall Score: ' + this.entry.overallScore.toFixed(1)
         + '\n\nPlease view this email in an HTML-capable client for the full report.';
  }

  /** @private — validates required fields before sending. */
  _validate() {
    if (!this.entry.agentEmail) {
      throw new Error('Cannot send QA email: agent email (column V) is empty for "'
                      + this.entry.agentName + '".');
    }
    if (!this.entry.managerEmail) {
      throw new Error('Cannot send QA email: manager email (column B) is empty.');
    }
  }
}
