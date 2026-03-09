/**
 * Mass Notifications — Looker Query
 *
 * UrlFetchApp wrappers for the Looker API 4.0.
 * Mirrors the "Active Occupants" tile on dashboard 4552
 * (model: landing, explore: dimreservation).
 *
 * All functions are private (_-suffixed) except where noted.
 */

// ── Core HTTP wrapper ─────────────────────────────────────────────────────────

/**
 * POSTs an inline query to the Looker API and returns the parsed JSON rows.
 *
 * @param {Object} queryDef — Looker query body (model, view, fields, filters, …)
 * @return {Object[]}
 */
function runLookerInlineQuery_(queryDef) {
  const url = `${LOOKER_BASE_URL}/api/${LOOKER_API_VERSION}/queries/run/json`;
  Logger.log(`[LookerQuery] POST ${url}`);
  Logger.log(`[LookerQuery] model="${queryDef.model}", view="${queryDef.view}", fields=${queryDef.fields.length}, filters=${JSON.stringify(queryDef.filters)}`);

  const resp = UrlFetchApp.fetch(url, {
    method:      'post',
    headers:     lookerHeaders_(),
    contentType: 'application/json',
    payload:     JSON.stringify(queryDef),
    muteHttpExceptions: true,
  });

  const code = resp.getResponseCode();
  Logger.log(`[LookerQuery] Response: HTTP ${code}`);

  if (code === 400) {
    Logger.log(`[LookerQuery] FAIL — 400 Bad Request. Likely a bad field name, filter key, or filter_expression. Body: ${resp.getContentText()}`);
    throw new Error(`Looker query failed (400 Bad Request): ${resp.getContentText()}`);
  }
  if (code === 401) {
    Logger.log('[LookerQuery] FAIL — 401 Unauthorized. Token may have expired mid-run despite caching.');
    throw new Error('Looker query failed (401 Unauthorized): token was rejected.');
  }
  if (code === 422) {
    Logger.log(`[LookerQuery] FAIL — 422 Unprocessable Entity. Check field names and filter syntax. Body: ${resp.getContentText()}`);
    throw new Error(`Looker query failed (422): ${resp.getContentText()}`);
  }
  if (code >= 500) {
    Logger.log(`[LookerQuery] FAIL — HTTP ${code}. Looker may be experiencing an outage. Body: ${resp.getContentText()}`);
    throw new Error(`Looker query failed (${code}): Looker returned a server error — it may be down or degraded.`);
  }
  if (code !== 200) {
    Logger.log(`[LookerQuery] FAIL — unexpected HTTP ${code}. Body: ${resp.getContentText()}`);
    throw new Error(`Looker query failed (${code}): ${resp.getContentText()}`);
  }

  const rows = JSON.parse(resp.getContentText());
  Logger.log(`[LookerQuery] Success — ${rows.length} row(s) returned.`);
  return rows;
}

// ── Domain query ──────────────────────────────────────────────────────────────

/**
 * Fetches active occupants for a given property, replicating the
 * "Active Occupants" tile logic from dashboard 4552:
 *   • Reservation platform  = Landing
 *   • Check-in date         < now   (already started)
 *   • Check-out date        = null OR > now  (not yet ended)
 *   • Currently occupied    = Yes
 *
 * The filter_expression mirrors the tile's LookML filter_expression exactly.
 *
 * @param {string} propertyName — Exact property name as it appears in Looker
 * @return {Object[]} Array of raw row objects keyed by Looker field name
 */
function fetchActiveOccupants_(propertyName) {
  Logger.log(`[LookerQuery] fetchActiveOccupants_() — property: "${propertyName}"`);
  return runLookerInlineQuery_({
    model:   LOOKER_MODEL,
    view:    LOOKER_EXPLORE,
    fields: [
      LFIELD.EMAIL,
      LFIELD.MEMBER_NAME,
      LFIELD.UNIT,
      LFIELD.OCC_NAME,
      LFIELD.PHONE,
      LFIELD.RES_ID,
      LFIELD.CHECK_IN,
      LFIELD.CHECK_OUT,
      LFIELD.PROPERTY,
      LFIELD.ALT_EMAIL_1,
      LFIELD.ALT_EMAIL_2,
      LFIELD.ALT_EMAIL_3,
      LFIELD.ALT_EMAIL_4,
    ],
    filters: {
      [LFIELD.PROPERTY]: propertyName,
      [LFIELD.PLATFORM]: 'Landing',
      [LFIELD.CHECK_IN]: 'before 0 minutes ago',
    },
    // Mirrors the tile's filter_expression: checkout null/future AND currently occupied
    filter_expression:
      '(matches_filter(${dimreservation.reservation_check_out_at_date}, `null`) ' +
      'OR matches_filter(${dimreservation.reservation_check_out_at_date}, `after 0 minutes ago`)) ' +
      'AND (matches_filter(${dimhome.currently_occupied}, `Yes`))',
    sorts: [`${LFIELD.CHECK_IN} desc`],
    limit: '500',
  });
}
