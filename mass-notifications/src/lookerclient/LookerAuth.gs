/**
 * Mass Notifications — Looker Authentication
 *
 * Handles OAuth2 client-credentials login against the Looker API.
 * The access token is cached in CacheService for 50 minutes
 * (token TTL is 60 min; 10-min buffer avoids mid-run expiry).
 *
 * Credentials are stored in Script Properties — never in sheet cells.
 * Run setupLookerCredentials() once from the menu to store them.
 */

// ── Token management ──────────────────────────────────────────────────────────

/**
 * Returns a valid Looker access token, fetching a new one if the cache
 * has expired.
 *
 * @return {string}
 */
function getLookerToken_() {
  const cache  = CacheService.getScriptCache();
  const cached = cache.get('looker_token');
  if (cached) {
    Logger.log('[LookerAuth] Using cached access token (still valid).');
    return cached;
  }

  Logger.log('[LookerAuth] No cached token — requesting a new one from Looker.');

  const props        = PropertiesService.getScriptProperties();
  const clientId     = props.getProperty('LOOKER_CLIENT_ID');
  const clientSecret = props.getProperty('LOOKER_CLIENT_SECRET');

  if (!clientId && !clientSecret) {
    Logger.log('[LookerAuth] FAIL — both LOOKER_CLIENT_ID and LOOKER_CLIENT_SECRET are missing from Script Properties.');
    throw new Error('Looker credentials not found. Run "Looker Sync → Setup credentials" first.');
  }
  if (!clientId) {
    Logger.log('[LookerAuth] FAIL — LOOKER_CLIENT_ID is missing from Script Properties.');
    throw new Error('Looker Client ID not found. Run "Looker Sync → Setup credentials" first.');
  }
  if (!clientSecret) {
    Logger.log('[LookerAuth] FAIL — LOOKER_CLIENT_SECRET is missing from Script Properties.');
    throw new Error('Looker Client Secret not found. Run "Looker Sync → Setup credentials" first.');
  }

  // Log the ID prefix only — never log the secret or full ID
  Logger.log(`[LookerAuth] Credentials present. Client ID starts with: "${clientId.slice(0, 4)}…"`);
  Logger.log(`[LookerAuth] POSTing to: ${LOOKER_BASE_URL}/api/${LOOKER_API_VERSION}/login`);

  const resp = UrlFetchApp.fetch(
    `${LOOKER_BASE_URL}/api/${LOOKER_API_VERSION}/login`,
    {
      method:      'post',
      contentType: 'application/x-www-form-urlencoded',
      payload:     `client_id=${encodeURIComponent(clientId)}&client_secret=${encodeURIComponent(clientSecret)}`,
      muteHttpExceptions: true,
    }
  );

  const code = resp.getResponseCode();
  Logger.log(`[LookerAuth] Login response: HTTP ${code}`);

  if (code === 401) {
    Logger.log('[LookerAuth] FAIL — 401 Unauthorized. Client ID or Secret is incorrect, or the API user has been disabled.');
    throw new Error(`Looker login failed (401 Unauthorized): credentials are invalid or the API user is disabled.`);
  }
  if (code === 404) {
    Logger.log(`[LookerAuth] FAIL — 404 Not Found. Check LOOKER_BASE_URL: "${LOOKER_BASE_URL}"`);
    throw new Error(`Looker login failed (404): instance URL may be wrong. Check LOOKER_BASE_URL in Constants.gs.`);
  }
  if (code !== 200) {
    Logger.log(`[LookerAuth] FAIL — unexpected HTTP ${code}. Response body: ${resp.getContentText()}`);
    throw new Error(`Looker login failed (${code}): ${resp.getContentText()}`);
  }

  const token = JSON.parse(resp.getContentText()).access_token;
  cache.put('looker_token', token, 50 * 60);
  Logger.log('[LookerAuth] Token obtained and cached for 50 minutes.');
  return token;
}

/**
 * Returns the Authorization header object for Looker API calls.
 * @return {{ Authorization: string }}
 */
function lookerHeaders_() {
  return { Authorization: `token ${getLookerToken_()}` };
}

// ── Setup & diagnostics ───────────────────────────────────────────────────────

/**
 * One-time setup: prompts for Client ID and Secret and saves them to
 * Script Properties. Run this from the menu; credentials are never stored
 * in the sheet.
 */
function setupLookerCredentials() {
  const ui = SpreadsheetApp.getUi();

  const idRes = ui.prompt(
    'Looker Setup (1/2)',
    'Enter your Looker API Client ID:',
    ui.ButtonSet.OK_CANCEL
  );
  if (idRes.getSelectedButton() !== ui.Button.OK) return;

  const secRes = ui.prompt(
    'Looker Setup (2/2)',
    'Enter your Looker API Client Secret:',
    ui.ButtonSet.OK_CANCEL
  );
  if (secRes.getSelectedButton() !== ui.Button.OK) return;

  PropertiesService.getScriptProperties().setProperties({
    LOOKER_CLIENT_ID:     idRes.getResponseText().trim(),
    LOOKER_CLIENT_SECRET: secRes.getResponseText().trim(),
  });

  safeAlert_('Looker credentials saved to Script Properties.');
}

/**
 * Quick connectivity test — verifies credentials and shows the
 * authenticated user's display name and email.
 */
function testLookerConnection() {
  Logger.log('[LookerAuth] testLookerConnection() started.');
  try {
    const url  = `${LOOKER_BASE_URL}/api/${LOOKER_API_VERSION}/user`;
    Logger.log(`[LookerAuth] GET ${url}`);

    const resp = UrlFetchApp.fetch(url, { headers: lookerHeaders_(), muteHttpExceptions: true });
    const code = resp.getResponseCode();
    Logger.log(`[LookerAuth] /user response: HTTP ${code}`);

    if (code === 401) {
      Logger.log('[LookerAuth] FAIL — 401 on /user. Token may have been revoked server-side.');
      throw new Error('HTTP 401: token was rejected. Try clearing the cache and re-running.');
    }
    if (code >= 500) {
      Logger.log(`[LookerAuth] FAIL — HTTP ${code}. Looker may be experiencing an outage. Body: ${resp.getContentText()}`);
      throw new Error(`HTTP ${code}: Looker returned a server error — it may be down or degraded.`);
    }
    if (code !== 200) {
      Logger.log(`[LookerAuth] FAIL — unexpected HTTP ${code}. Body: ${resp.getContentText()}`);
      throw new Error(`HTTP ${code}: ${resp.getContentText()}`);
    }

    const user = JSON.parse(resp.getContentText());
    Logger.log(`[LookerAuth] Connection OK — authenticated as: ${user.display_name} (${user.email}), role IDs: ${(user.role_ids || []).join(', ') || 'none'}`);
    safeAlert_(`Looker connection OK\nConnected as: ${user.display_name} (${user.email})`);
  } catch (e) {
    Logger.log(`[LookerAuth] testLookerConnection() caught error: ${e.message}`);
    safeAlert_(`Looker connection failed: ${e.message}`);
  }
}
