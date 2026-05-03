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

/**
 * Builds the token map for a Move-In Flow row.
 *
 * Includes all global tokens plus per-row Move-In fields used by
 * MoveInCard, the subject template, and the greeting.
 *
 * `_occupants` is the parsed occupants array — consumed directly by
 * MoveInCard.render(); not intended for {{}} substitution.
 *
 * @param {Object} cfg — result of loadConfig_()
 * @param {Object} rec — record from getMoveInTargetRows_()
 * @return {Object}
 */
function buildMoveInTokens_(cfg, rec) {
  const moveIn  = formatMoveInDate_(rec.moveInDate,  cfg.timezone);
  const moveOut = formatMoveInDate_(rec.moveOutDate, cfg.timezone);
  return {
    ...buildGlobalTokens_(cfg),
    property_name:    rec.propertyName    || '',
    apartment_number: rec.aptNumber       || '',
    member_name:      rec.memberName      || '',
    member_email:     rec.memberEmail     || '',
    member_phone:     rec.memberPhone     || '',
    move_in_date:     moveIn,
    move_out_date:    moveOut,
    vehicle_info:     rec.vehicleInfo     || '',
    pet_info:         rec.petInfo         || '',
    area_mgr_name:    rec.areaMgrName     || '',
    area_mgr_phone:   rec.areaMgrPhone    || '',
    area_mgr_email:   rec.areaMgrEmail    || '',
    reservation_id:   rec.reservationId   || '',
    _occupants:       rec.occupants       || [],
  };
}

/**
 * Formats a Move-In date as MM/dd/yyyy. Accepts Date objects, ISO strings,
 * and pre-formatted strings (passed through). Empty input → empty string.
 */
function formatMoveInDate_(val, tz) {
  if (val == null || val === '') return '';
  if (Object.prototype.toString.call(val) === '[object Date]' && !isNaN(val)) {
    return Utilities.formatDate(val, tz, 'MM/dd/yyyy');
  }
  // Already a string (e.g. operator typed "04/06/2025") — pass through.
  return String(val).trim();
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
    return wrapWithBranding_(
      cfg,
      normalizeTelLinks_(renderWithTokens_(cfg.bodyFullHtml, tokens) + (cfg.signatureHtml || ''))
    );
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

  return wrapWithBranding_(cfg, normalizeTelLinks_(body + (cfg.signatureHtml || '')));
}

/**
 * Wraps the rendered email in an optional cream (or other) background and
 * header logo. Both are opt-in via Config:
 *   email_background_color — e.g. "#F5F1E8"
 *   email_header_image_url — public URL of a Landing wordmark .png
 *
 * When neither is set, returns the body unchanged. Used by buildHtmlBody_
 * (incl. the body_full_html fast path) so every send mode picks it up if
 * the operator opts in. Move-In Notification ships with cream on by default.
 */
function wrapWithBranding_(cfg, html) {
  const bg     = cfg.emailBackgroundColor || '';
  const imgUrl = cfg.emailHeaderImageUrl  || '';
  if (!bg && !imgUrl) return html;

  const header = imgUrl
    ? `<div style="text-align:center;padding:0 0 16px 0;">` +
        `<img src="${escapeHtml_(imgUrl)}" alt="Landing" ` +
        `style="max-width:240px;height:auto;border:0;display:inline-block;">` +
      `</div>`
    : '';

  // Two nested tables — the outer is full-width transparent (so the user's
  // mail client renders its own page background), the inner is the cream
  // content block, capped at 640px and centered for readability.
  // 640 is the de facto standard for B2B/transactional email content width.
  return [
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:transparent;">`,
      `<tr><td align="center" style="padding:0;">`,
        `<table role="presentation" cellpadding="0" cellspacing="0" border="0" `,
        `style="max-width:640px;width:100%;background-color:${bg || '#FFFFFF'};border-radius:8px;">`,
          `<tr><td style="padding:32px 36px;font-family:Arial,Helvetica,sans-serif;">`,
            header,
            `<div>`, html, `</div>`,
          `</td></tr>`,
        `</table>`,
      `</td></tr>`,
    `</table>`,
  ].join('');
}

// ── Misc composition helpers ──────────────────────────────────────────────────

/** Combines manager_email and cc_extra into a single CC string. */
function combineCc_(cfg) {
  return [cfg.managerEmail || '', cfg.ccExtra || ''].filter(Boolean).join(',');
}
