/**
 * Mass Notifications — Tokenizer
 *
 * Two distinct responsibilities kept deliberately separate:
 *
 *   1. TOKEN ENGINE  — renderWithTokens_()
 *      Replaces {{token}} and {{token | fallback}} placeholders in any
 *      template string.  Pure function; no side-effects.
 *
 *   2. TOKEN MAP BUILDERS — buildGlobalTokens_() / buildPerRowTokens_()
 *      Assemble the flat {key: value} map that is passed into the engine.
 *
 *   3. BODY COMPOSER — buildHtmlBody_()
 *      Assembles the final email HTML from config parts + tokens + optional
 *      notification card.  This is the only place that knows about email
 *      structure; the token engine itself knows nothing about email.
 */

// ── Token engine ──────────────────────────────────────────────────────────────

/**
 * Replaces every {{token}} and {{token | fallback}} in `template` with the
 * corresponding value from `tokens`.
 *
 * Rules:
 *   • {{key}}           → tokens[key] or '' when missing/empty
 *   • {{key | default}} → tokens[key] or 'default' when missing/empty
 *   • Token values that are already HTML pass through sanitizeKeepBasicHtml_.
 *
 * @param {string} template
 * @param {Object} tokens   — flat key→value map
 * @return {string}
 */
function renderWithTokens_(template, tokens) {
  if (!template) return '';
  return String(template).replace(
    /\{\{\s*([a-zA-Z0-9_]+)\s*(?:\|\s*([^}]+))?\s*\}\}/g,
    (_, key, fallback) => {
      const val = tokens[key];
      const out = (val !== undefined && val !== null && String(val).trim() !== '')
        ? val
        : (fallback != null ? fallback.trim() : '');
      return sanitizeKeepBasicHtml_(out);
    }
  );
}

// ── Token map builders ────────────────────────────────────────────────────────

/**
 * Builds the global token map — values that are the same for every recipient
 * in a given send run.
 *
 * @param {Object} cfg — result of loadConfig_()
 * @return {Object}
 */
function buildGlobalTokens_(cfg) {
  const dateRange = formatDateRange_(cfg.start, cfg.end, cfg.timezone);
  const today     = Utilities.formatDate(new Date(), cfg.timezone, 'EEE, MMM d, yyyy');
  return {
    property_name:  cfg.propertyName  || '',
    event_name:     cfg.eventName     || '',
    date_range:     dateRange,
    today,
    manager_email:  cfg.managerEmail  || '',
    manager_name:   cfg.managerName   || '',
  };
}

/**
 * Extends the global token map with per-recipient values.
 *
 * @param {Object} cfg — result of loadConfig_()
 * @param {Object} t   — { email, name, unit } from getTargetRows_()
 * @return {Object}
 */
function buildPerRowTokens_(cfg, t) {
  const firstName = t.name ? String(t.name).trim().split(/\s+/)[0] : '';
  return {
    ...buildGlobalTokens_(cfg),
    member_email: t.email || '',
    member_name:  t.name  || '',
    first_name:   firstName || 'Resident',
    unit:         t.unit  || '',
  };
}

// ── Body composer ─────────────────────────────────────────────────────────────

/**
 * Assembles the complete HTML email body from config parts and tokens.
 *
 * Composition order:
 *   1. [Fast path] body_full_html override → return immediately + signature
 *   2. Greeting
 *   3. Disclaimer (if include_disclaimer = YES)
 *   4. Notification card (if notification_card key is set in Config)
 *   5. Body intro
 *   6. Unit line (INDIVIDUAL mode only, if include_unit_line = YES)
 *   7. Closing
 *   8. Signature
 *
 * @param {Object}      cfg     — result of loadConfig_()
 * @param {Object}      tokens  — result of buildPerRowTokens_() or buildGlobalTokens_()
 * @param {string|null} unitStr — unit string for the current recipient (or null for BCC)
 * @return {string} — complete HTML ready for GmailApp.sendEmail htmlBody
 */
function buildHtmlBody_(cfg, tokens, unitStr) {
  // Fast path: full override
  if (cfg.bodyFullHtml && String(cfg.bodyFullHtml).trim()) {
    return normalizeTelLinks_(renderWithTokens_(cfg.bodyFullHtml, tokens) + (cfg.signatureHtml || ''));
  }

  const parts = [];

  parts.push(renderWithTokens_(cfg.greetingTemplate, tokens));

  if (cfg.includeDisclaimer) {
    const dis = cfg.disclaimerHtml ||
      '<p><em>This is an automated mass notification to all active residents.</em></p>';
    parts.push(renderWithTokens_(dis, tokens));
  }

  // Notification card slot
  if (cfg.notificationCard) {
    const cardHtml = buildCard_(cfg.notificationCard, tokens);
    if (cardHtml) parts.push(cardHtml);
  }

  parts.push(renderWithTokens_(cfg.bodyIntroHtml, tokens));

  if (cfg.includeUnitLine && unitStr) {
    parts.push(`<p><strong>Your unit number is:</strong> ${sanitize_(unitStr)}</p>`);
  }

  parts.push(renderWithTokens_(cfg.closingHtml, tokens));

  // Quill wraps each paragraph in a bare <p> tag. Browser-default <p> margins
  // (~16px top + bottom) make multi-paragraph bodies appear double-spaced in
  // email clients. Apply a compact inline margin across all template sections
  // so spacing is consistent. Signature is excluded — it has its own formatting.
  // Only bare <p> tags are affected; styled <p> tags (e.g. from cards) pass through.
  const body = parts.filter(Boolean).join('')
    .replace(/<p>/gi, '<p style="margin:0 0 0.8em 0;">');

  return normalizeTelLinks_(body + (cfg.signatureHtml || ''));
}

// ── Misc composition helpers ──────────────────────────────────────────────────

/** Combines manager_email and cc_extra into a single CC string. */
function combineCc_(cfg) {
  return [cfg.managerEmail || '', cfg.ccExtra || ''].filter(Boolean).join(',');
}
