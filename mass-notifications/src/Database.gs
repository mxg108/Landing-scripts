/**
 * Mass Notifications -Database
 * JDBC connection to Railway PostgreSQL for archiving campaigns and recipients.
 *
 * Credentials are stored in Script Properties (not in code):
 *   DB_URL  = jdbc:postgresql://host:port/dbname
 *   DB_USER = postgres
 *   DB_PASS = (password)
 *
 * Set these manually in Project Settings → Script Properties.
 */


// ── Connection helper ────────────────────────────────────────────────────────

/**
 * Opens a JDBC connection to the Railway PostgreSQL database.
 * Caller is responsible for closing the connection.
 *
 * @return {JdbcConnection}
 */
function getDbConnection_() {
  const props = PropertiesService.getScriptProperties();
  const url   = props.getProperty('DB_URL');
  const user  = props.getProperty('DB_USER');
  const pass  = props.getProperty('DB_PASS');

  if (!url || !user || !pass) {
    throw new Error('DB credentials not set in Script Properties.');
  }

  // Railway free tier can cold-start the DB. Retry once after a brief pause.
  try {
    return Jdbc.getConnection(url, user, pass);
  } catch (e) {
    Logger.log('DB connection attempt 1 failed, retrying in 3s: ' + e.message);
    Utilities.sleep(3000);
    return Jdbc.getConnection(url, user, pass);
  }
}

// ── Archive to DB ────────────────────────────────────────────────────────────

/**
 * Archives all recipient rows (sent + skipped) and the campaign metadata
 * to the PostgreSQL database.
 *
 * Called by archiveAndClearRecipientsQuiet_() instead of copying to a
 * hidden sheet.
 *
 * @param {Object}  cfg    -result of loadConfig_()
 * @param {string}  shName -name of the recipients sheet
 * @param {Array[]} data   -2D array of all data rows (excluding header),
 *                           columns: [email, name, unit, status, lastSent, notes, attachIds]
 * @param {string|null} [subject=null] -email subject line (optional; null for archive-only calls)
 * @return {number} The campaign ID inserted into the DB
 */
function archiveCampaignToDb_(cfg, shName, data, subject) {
  const conn = getDbConnection_();

  try {
    // ── Insert campaign row ──────────────────────────────────────────────
    const runId = `ARCHIVE-${Utilities.formatDate(new Date(), cfg.timezone, 'yyyyMMdd_HHmmss')}`;
    const actor = Session.getActiveUser().getEmail() || 'unknown';

    const cfgSnapshot = JSON.stringify({
      version:       typeof VERSION !== 'undefined' ? VERSION : '',
      sendMode:      cfg.sendMode,
      managerEmail:  cfg.managerEmail,
      propertyName:  cfg.propertyName,
      eventName:     cfg.eventName,
      window: [
        Utilities.formatDate(cfg.start, cfg.timezone, 'yyyy-MM-dd'),
        Utilities.formatDate(cfg.end,   cfg.timezone, 'yyyy-MM-dd'),
      ],
      timezone:      cfg.timezone,
    });

    const sentCount = data.filter(r => {
      const status = String(r[3] || '').trim().toUpperCase();
      return status === 'SENT';
    }).length;

    const campaignSql =
      'INSERT INTO mass_notifications.campaigns ' +
      '(run_id, mode, recipients_sheet, emails_sent, total_rows, actor, subject, config_snapshot, completed) ' +
      'VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?) RETURNING id';

    const campaignStmt = conn.prepareStatement(campaignSql);
    campaignStmt.setString(1, runId);
    campaignStmt.setString(2, 'ARCHIVE');
    campaignStmt.setString(3, shName);
    campaignStmt.setInt(4, sentCount);
    campaignStmt.setInt(5, data.length);
    campaignStmt.setString(6, actor);
    if (subject) {
      campaignStmt.setString(7, subject);
    } else {
      campaignStmt.setNull(7, Jdbc.Types.VARCHAR);
    }
    campaignStmt.setString(8, cfgSnapshot);
    campaignStmt.setBoolean(9, true);

    const rs = campaignStmt.executeQuery();
    rs.next();
    const campaignId = rs.getInt(1);
    rs.close();
    campaignStmt.close();

    // ── Batch-insert recipient rows ──────────────────────────────────────
    if (data.length > 0) {
      const recipientSql =
        'INSERT INTO mass_notifications.recipients ' +
        '(campaign_id, sheet_row, email, name, unit, status_after, last_sent_after, notes) ' +
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)';

      const recipientStmt = conn.prepareStatement(recipientSql);

      for (let i = 0; i < data.length; i++) {
        const row    = data[i];
        const email  = String(row[0] || '').trim();
        const name   = String(row[1] || '').trim();
        const unit   = String(row[2] || '').trim();
        const status = String(row[3] || '').trim();
        const lastSent = row[4];
        const notes  = String(row[5] || '').trim();

        recipientStmt.setInt(1, campaignId);
        recipientStmt.setInt(2, i + 2);  // 1-based, header is row 1
        recipientStmt.setString(3, email || '(empty)');
        recipientStmt.setString(4, name);
        recipientStmt.setString(5, unit);
        recipientStmt.setString(6, status);

        // Handle lastSent -could be a Date or empty
        if (lastSent && Object.prototype.toString.call(lastSent) === '[object Date]' && !isNaN(lastSent)) {
          recipientStmt.setTimestamp(7, Jdbc.newTimestamp(lastSent.getTime()));
        } else {
          recipientStmt.setNull(7, Jdbc.Types.TIMESTAMP);
        }

        recipientStmt.setString(8, notes);
        recipientStmt.addBatch();
      }

      recipientStmt.executeBatch();
      recipientStmt.close();
    }

    return campaignId;

  } finally {
    conn.close();
  }
}

// ── Query helpers ────────────────────────────────────────────────────────────

/**
 * Returns all recipients from campaigns archived on a given date.
 * Date should be in YYYY-MM-DD format (e.g., '2026-03-16').
 *
 * Run from the Apps Script editor. Results are logged.
 *
 * @param {string} dateStr -date to query in YYYY-MM-DD format
 * @return {Object[]} Array of { runId, actor, email, name, unit, status, sentAt }
 */
function fetchByDate(dateStr) {
  if (!dateStr) { Logger.log('Usage: fetchByDate("2026-03-16")'); return []; }

  const conn = getDbConnection_();
  try {
    const sql =
      'SELECT c.run_id, c.actor, r.email, r.name, r.unit, ' +
      '       r.status_after, r.last_sent_after ' +
      'FROM mass_notifications.recipients r ' +
      'JOIN mass_notifications.campaigns c ON c.id = r.campaign_id ' +
      'WHERE c.created_at::date = ? ' +
      'ORDER BY c.created_at, r.sheet_row';

    const stmt = conn.prepareStatement(sql);
    stmt.setString(1, dateStr);
    const rs = stmt.executeQuery();

    const results = [];
    while (rs.next()) {
      results.push({
        runId:  rs.getString(1),
        actor:  rs.getString(2),
        email:  rs.getString(3),
        name:   rs.getString(4),
        unit:   rs.getString(5),
        status: rs.getString(6),
        sentAt: rs.getString(7),
      });
    }
    rs.close();
    stmt.close();

    Logger.log(`fetchByDate("${dateStr}"): ${results.length} recipient(s) found`);
    results.forEach(r => Logger.log(`  ${r.email} | ${r.name} | unit ${r.unit} | ${r.status}`));
    return results;
  } finally {
    conn.close();
  }
}

/**
 * Returns the full notification history for a given email address.
 * Shows every campaign the recipient appeared in, whether sent or skipped.
 *
 * Run from the Apps Script editor. Results are logged.
 *
 * @param {string} email -recipient email to search for
 * @return {Object[]} Array of { date, runId, property, event, status, actor }
 */
function fetchByRecipient(email) {
  if (!email) { Logger.log('Usage: fetchByRecipient("jane@example.com")'); return []; }

  const conn = getDbConnection_();
  try {
    const sql =
      'SELECT c.created_at, c.run_id, ' +
      '       c.config_snapshot->>\'propertyName\' AS property, ' +
      '       c.config_snapshot->>\'eventName\' AS event, ' +
      '       r.status_after, c.actor ' +
      'FROM mass_notifications.recipients r ' +
      'JOIN mass_notifications.campaigns c ON c.id = r.campaign_id ' +
      'WHERE LOWER(r.email) = LOWER(?) ' +
      'ORDER BY c.created_at DESC';

    const stmt = conn.prepareStatement(sql);
    stmt.setString(1, email.trim());
    const rs = stmt.executeQuery();

    const results = [];
    while (rs.next()) {
      results.push({
        date:     rs.getString(1),
        runId:    rs.getString(2),
        property: rs.getString(3),
        event:    rs.getString(4),
        status:   rs.getString(5),
        actor:    rs.getString(6),
      });
    }
    rs.close();
    stmt.close();

    Logger.log(`fetchByRecipient("${email}"): ${results.length} campaign(s) found`);
    results.forEach(r => Logger.log(`  ${r.date} | ${r.property} | ${r.event} | ${r.status}`));
    return results;
  } finally {
    conn.close();
  }
}

/**
 * Returns all campaigns and recipients for a given property name.
 * Searches the config_snapshot JSONB field for a matching propertyName.
 *
 * Run from the Apps Script editor. Results are logged.
 *
 * @param {string} property -property name to search for (case-insensitive partial match)
 * @return {Object[]} Array of { date, runId, event, emailsSent, totalRows, actor }
 */
function fetchByProperty(property) {
  if (!property) { Logger.log('Usage: fetchByProperty("The Grand")'); return []; }

  const conn = getDbConnection_();
  try {
    const sql =
      'SELECT c.created_at, c.run_id, ' +
      '       c.config_snapshot->>\'eventName\' AS event, ' +
      '       c.config_snapshot->>\'propertyName\' AS property, ' +
      '       c.emails_sent, c.total_rows, c.actor ' +
      'FROM mass_notifications.campaigns c ' +
      'WHERE LOWER(c.config_snapshot->>\'propertyName\') LIKE LOWER(?) ' +
      'ORDER BY c.created_at DESC';

    const stmt = conn.prepareStatement(sql);
    stmt.setString(1, '%' + property.trim() + '%');
    const rs = stmt.executeQuery();

    const results = [];
    while (rs.next()) {
      results.push({
        date:       rs.getString(1),
        runId:      rs.getString(2),
        event:      rs.getString(3),
        property:   rs.getString(4),
        emailsSent: rs.getInt(5),
        totalRows:  rs.getInt(6),
        actor:      rs.getString(7),
      });
    }
    rs.close();
    stmt.close();

    Logger.log(`fetchByProperty("${property}"): ${results.length} campaign(s) found`);
    results.forEach(r => Logger.log(`  ${r.date} | ${r.property} | ${r.event} | sent: ${r.emailsSent}/${r.totalRows}`));
    return results;
  } finally {
    conn.close();
  }
}

/**
 * Returns all campaigns triggered by a given actor (sender).
 * Matches by email address (case-insensitive partial match).
 *
 * @param {string} actor -actor email to search for
 * @return {Object[]} Array of { date, runId, property, event, mode, emailsSent, totalRows }
 */
function fetchByActor(actor) {
  if (!actor) { Logger.log('Usage: fetchByActor("maximiliano")'); return []; }

  const conn = getDbConnection_();
  try {
    const sql =
      'SELECT c.created_at, c.run_id, ' +
      '       c.config_snapshot->>\'propertyName\' AS property, ' +
      '       c.config_snapshot->>\'eventName\' AS event, ' +
      '       c.mode, c.emails_sent, c.total_rows, c.actor ' +
      'FROM mass_notifications.campaigns c ' +
      'WHERE LOWER(c.actor) LIKE LOWER(?) ' +
      'ORDER BY c.created_at DESC';

    const stmt = conn.prepareStatement(sql);
    stmt.setString(1, '%' + actor.trim() + '%');
    const rs = stmt.executeQuery();

    const results = [];
    while (rs.next()) {
      results.push({
        date:       rs.getString(1),
        runId:      rs.getString(2),
        property:   rs.getString(3),
        event:      rs.getString(4),
        mode:       rs.getString(5),
        emailsSent: rs.getInt(6),
        totalRows:  rs.getInt(7),
        actor:      rs.getString(8),
      });
    }
    rs.close();
    stmt.close();

    Logger.log('fetchByActor("' + actor + '"): ' + results.length + ' campaign(s) found');
    results.forEach(r => Logger.log('  ' + r.date + ' | ' + r.property + ' | ' + r.event + ' | ' + r.mode + ' | sent: ' + r.emailsSent + '/' + r.totalRows));
    return results;
  } finally {
    conn.close();
  }
}

// ── Migration: archive sheets → DB ───────────────────────────────────────────

/**
 * One-time migration: reads all hidden Archive_* sheets and their matching
 * Run_Log entries, then inserts them into the PostgreSQL database.
 *
 * For each archive sheet:
 *   1. Parses the timestamp from the sheet name (Archive_<name>_yyyyMMdd_HHmmss)
 *   2. Finds the most recent completed Run_Log entry before that timestamp
 *   3. Creates a campaign row using Run_Log metadata (actor, mode, subject, config_snapshot)
 *   4. Inserts all rows from the archive sheet as recipients
 *   5. Enriches recipient statuses from Run_Log RowStates JSON when available
 *
 * Safe to re-run -skips archive sheets whose run_id already exists in the DB.
 *
 * Run from the Apps Script editor. Progress is logged to Logger.
 */
function migrateArchivesToDb() {
  const ss       = SpreadsheetApp.getActive();
  const sheets   = ss.getSheets();
  const pattern  = /^Archive_.+_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/;

  // ── Load all Run_Log entries ───────────────────────────────────────────
  const logSheet = ss.getSheetByName(RUN_LOG_SHEET);
  const logRows  = [];
  if (logSheet && logSheet.getLastRow() >= 2) {
    const logData = logSheet.getRange(2, 1, logSheet.getLastRow() - 1, 11).getValues();
    logData.forEach(row => {
      const ts = row[0];
      if (!ts || !(ts instanceof Date)) return;
      logRows.push({
        timestamp:    ts,
        runId:        String(row[1] || ''),
        mode:         String(row[2] || ''),
        shName:       String(row[3] || ''),
        count:        Number(row[4] || 0),
        actor:        String(row[5] || ''),
        subject:      String(row[6] || ''),
        cfgSnapshot:  String(row[7] || ''),
        rowStates:    String(row[8] || ''),
        notes:        String(row[9] || ''),
        completed:    String(row[10]).toUpperCase() === 'TRUE',
      });
    });
    // Sort oldest first
    logRows.sort((a, b) => a.timestamp - b.timestamp);
  }
  Logger.log(`Loaded ${logRows.length} Run_Log entries`);

  // ── Collect archive sheets ─────────────────────────────────────────────
  const archives = [];
  sheets.forEach(sh => {
    if (!sh.isSheetHidden()) return;
    const m = sh.getName().match(pattern);
    if (!m) return;
    const archiveDate = new Date(
      parseInt(m[1]), parseInt(m[2]) - 1, parseInt(m[3]),
      parseInt(m[4]), parseInt(m[5]),      parseInt(m[6])
    );
    archives.push({ sheet: sh, name: sh.getName(), date: archiveDate });
  });
  archives.sort((a, b) => a.date - b.date);
  Logger.log(`Found ${archives.length} archive sheets to migrate`);

  if (!archives.length) return;

  // ── Check which run_ids already exist in DB ────────────────────────────
  const existingIds = new Set();
  const checkConn = getDbConnection_();
  try {
    const checkStmt = checkConn.createStatement();
    const checkRs   = checkStmt.executeQuery(
      'SELECT run_id FROM mass_notifications.campaigns'
    );
    while (checkRs.next()) existingIds.add(checkRs.getString(1));
    checkRs.close();
    checkStmt.close();
  } finally {
    checkConn.close();
  }

  // ── Migrate each archive (fresh connection per sheet) ──────────────────
  let migrated = 0;
  let skipped  = 0;

  for (const archive of archives) {
    const archiveRunId = `MIGRATED-${archive.name}`;

    if (existingIds.has(archiveRunId)) {
      skipped++;
      continue;
    }

    // Find the most recent completed Run_Log entry before this archive
    let matchedLog = null;
    for (let i = logRows.length - 1; i >= 0; i--) {
      const lr = logRows[i];
      if (lr.completed && lr.timestamp <= archive.date &&
          lr.mode !== 'UNDO' && lr.mode !== 'DRYRUN_DRAFTS') {
        matchedLog = lr;
        break;
      }
    }

    // Read archive sheet data
    const lastRow = archive.sheet.getLastRow();
    const lastCol = archive.sheet.getLastColumn();
    if (lastRow < 2 || lastCol < 1) {
      Logger.log(`  Skipping ${archive.name}: empty`);
      skipped++;
      continue;
    }

    const data = archive.sheet.getRange(2, 1, lastRow - 1, Math.min(lastCol, COL_MAX)).getValues();

    // Parse RowStates from matched log entry for status enrichment
    let rowStatesMap = {};
    if (matchedLog && matchedLog.rowStates) {
      try {
        const states = JSON.parse(matchedLog.rowStates);
        if (Array.isArray(states)) {
          states.forEach(s => {
            if (s && s.email) rowStatesMap[s.email.toLowerCase()] = s;
          });
        }
      } catch (_) { /* malformed JSON, skip enrichment */ }
    }

    // ── Fresh connection for this archive ────────────────────────────
    const conn = getDbConnection_();
    try {
      // ── Insert campaign ────────────────────────────────────────────
      const campaignSql =
        'INSERT INTO mass_notifications.campaigns ' +
        '(run_id, mode, recipients_sheet, emails_sent, total_rows, actor, ' +
        ' subject, config_snapshot, notes, completed, created_at) ' +
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?)  RETURNING id';

      const cStmt = conn.prepareStatement(campaignSql);
      cStmt.setString(1, archiveRunId);
      cStmt.setString(2, matchedLog ? matchedLog.mode : 'UNKNOWN');
      cStmt.setString(3, matchedLog ? matchedLog.shName : archive.name);

      const sentCount = data.filter(r =>
        String(r[3] || '').trim().toUpperCase() === 'SENT'
      ).length;
      cStmt.setInt(4, sentCount);
      cStmt.setInt(5, data.length);
      cStmt.setString(6, matchedLog ? matchedLog.actor : 'unknown');
      cStmt.setString(7, matchedLog ? matchedLog.subject : null);

      if (matchedLog && matchedLog.cfgSnapshot) {
        cStmt.setString(8, matchedLog.cfgSnapshot);
      } else {
        cStmt.setNull(8, Jdbc.Types.VARCHAR);
      }

      cStmt.setString(9, 'Migrated from hidden sheet: ' + archive.name);
      cStmt.setBoolean(10, true);
      cStmt.setTimestamp(11, Jdbc.newTimestamp(archive.date.getTime()));

      const cRs = cStmt.executeQuery();
      cRs.next();
      const campaignId = cRs.getInt(1);
      cRs.close();
      cStmt.close();

      // ── Insert recipients ──────────────────────────────────────────
      if (data.length > 0) {
        const rSql =
          'INSERT INTO mass_notifications.recipients ' +
          '(campaign_id, sheet_row, email, name, unit, ' +
          ' status_before, status_after, last_sent_before, last_sent_after, notes) ' +
          'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)';

        const rStmt = conn.prepareStatement(rSql);

        for (let i = 0; i < data.length; i++) {
          const row    = data[i];
          const email  = String(row[0] || '').trim();
          const name   = String(row[1] || '').trim();
          const unit   = String(row[2] || '').trim();
          const status = String(row[3] || '').trim();
          const lastSent = row[4];
          const notes  = row[5] ? String(row[5]).trim() : '';

          const enriched = email ? rowStatesMap[email.toLowerCase()] : null;

          rStmt.setInt(1, campaignId);
          rStmt.setInt(2, i + 2);
          rStmt.setString(3, email || '(empty)');
          rStmt.setString(4, name);
          rStmt.setString(5, unit);

          rStmt.setString(6, enriched ? (enriched.prevStatus || '') : '');
          rStmt.setString(7, status);

          if (enriched && enriched.prevLastSent) {
            const prev = new Date(enriched.prevLastSent);
            if (!isNaN(prev)) {
              rStmt.setTimestamp(8, Jdbc.newTimestamp(prev.getTime()));
            } else {
              rStmt.setNull(8, Jdbc.Types.TIMESTAMP);
            }
          } else {
            rStmt.setNull(8, Jdbc.Types.TIMESTAMP);
          }

          if (lastSent && lastSent instanceof Date && !isNaN(lastSent)) {
            rStmt.setTimestamp(9, Jdbc.newTimestamp(lastSent.getTime()));
          } else {
            rStmt.setNull(9, Jdbc.Types.TIMESTAMP);
          }

          rStmt.setString(10, notes);
          rStmt.addBatch();
        }

        rStmt.executeBatch();
        rStmt.close();
      }

      migrated++;
      if (migrated % 25 === 0) Logger.log(`  Progress: ${migrated} migrated...`);

    } catch (e) {
      Logger.log(`  ERROR on ${archive.name}: ${e.message}`);
    } finally {
      conn.close();
    }
  }

  Logger.log(`Migration complete: ${migrated} archived, ${skipped} skipped`);
}

// ── Connection test ──────────────────────────────────────────────────────────

/**
 * Quick connectivity test -run from the Apps Script editor.
 * Logs the Postgres version and table counts.
 */
function testDbConnection() {
  const conn = getDbConnection_();
  try {
    const stmt = conn.createStatement();

    const vr = stmt.executeQuery('SELECT version()');
    vr.next();
    Logger.log('Connected: ' + vr.getString(1));
    vr.close();

    const cr = stmt.executeQuery(
      'SELECT count(*) FROM mass_notifications.campaigns'
    );
    cr.next();
    Logger.log('Campaigns in DB: ' + cr.getInt(1));
    cr.close();

    const rr = stmt.executeQuery(
      'SELECT count(*) FROM mass_notifications.recipients'
    );
    rr.next();
    Logger.log('Recipients in DB: ' + rr.getInt(1));
    rr.close();

    stmt.close();
    safeAlert_('DB connection OK. Check Logger for details.');
  } finally {
    conn.close();
  }
}

// ── Results renderer (used by Database menu UI in UI.gs) ─────────────────────

/**
 * Renders query results as a styled HTML table for display in a modal.
 * Called by the Database menu prompt functions in UI.gs.
 *
 * @param {string}   title   - heading shown above the table
 * @param {string[]} headers - column header labels
 * @param {Array[]}  rows    - 2D array of cell values
 * @return {string}  HTML string
 */
function renderQueryResults_(title, headers, rows) {
  const thStyle = 'padding:6px 10px;background:#15192D;color:#fff;text-align:left;font-size:12px;';
  const tdStyle = 'padding:5px 10px;border-bottom:1px solid #e0e0e0;font-size:12px;';
  const altBg   = 'background:#f8f9fa;';

  let html = '<div style="font-family:Arial,Helvetica,sans-serif;padding:12px;">';
  html += '<h2 style="margin:0 0 12px;">' + escapeHtml_(title) + '</h2>';

  if (!rows.length) {
    html += '<p style="color:#666;">No results found.</p></div>';
    return html;
  }

  html += '<p style="color:#666;margin:0 0 8px;">' + rows.length + ' result(s)</p>';
  html += '<table style="border-collapse:collapse;width:100%;">';
  html += '<tr>' + headers.map(function(h) { return '<th style="' + thStyle + '">' + escapeHtml_(h) + '</th>'; }).join('') + '</tr>';

  rows.forEach(function(row, i) {
    var bg = i % 2 === 1 ? altBg : '';
    html += '<tr>' + row.map(function(v) { return '<td style="' + tdStyle + bg + '">' + escapeHtml_(v != null ? String(v) : '') + '</td>'; }).join('') + '</tr>';
  });

  html += '</table></div>';
  return html;
}
