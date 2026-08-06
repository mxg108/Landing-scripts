/**
 * Mass Notifications — GAS Mail Dispatcher (payload mode)
 *
 * Deployed under member.support@hellolanding.com (PRD D3). A dumb,
 * stateless mail arm for the Sandy app `mass-notifications`: the Sandy
 * dispatch workflow POSTs fully-rendered messages here; this script
 * resolves Drive attachments and sends via GmailApp. No sheets, no
 * state, no rendering logic.
 *
 * Request (JSON body):
 *   { "secret":   "<shared secret, must match Script Property DISPATCH_SECRET>",
 *     "mode":     "send" | "draft" | "health",
 *     "messages": [                          // required for send/draft, max 10
 *       { "to":        "a@b.com",           // required
 *         "cc":        "x@y.com, z@w.com",  // optional
 *         "bcc":       "",                  // optional
 *         "replyTo":   "",                  // optional
 *         "senderName": "Landing Notifications",   // optional display name
 *         "subject":   "...",               // required
 *         "htmlBody":  "<p>...</p>",        // required
 *         "plainText": "...",               // optional fallback
 *         "attachmentFileIds": ["<driveId>", ...]  // optional; a single
 *                                           // folder ID expands to its
 *                                           // direct children (no subfolders)
 *       }, ...
 *     ]
 *   }
 *
 * Response (JSON):
 *   health:      { "status": "ok", "account": "...", "quotaRemaining": N,
 *                  "version": "..." }
 *   send/draft:  { "status": "ok" | "partial" | "error",
 *                  "quotaRemaining": N,
 *                  "results": [ { "to": "...", "ok": true|false,
 *                                 "error": "..."? }, ... ] }
 *   bad auth:    { "status": "error", "message": "unauthorized" }  (always
 *                 HTTP 200 — GAS web apps cannot set status codes; callers
 *                 must check the JSON body)
 *
 * Attachment error strings match the legacy app exactly (parity contract):
 *   ATTACH_NOT_FOUND id=<id>
 *   ATTACH_TOO_LARGE total=<bytes>
 */

var VERSION = 'mn-dispatcher v1.0.0';
var MAX_ATTACH_BYTES = 20 * 1024 * 1024; // 20 MB (Gmail hard limit ~25 MB)
var MAX_MESSAGES_PER_CALL = 10;

function doPost(e) {
  var payload;
  try {
    payload = JSON.parse(e.postData.contents);
  } catch (err) {
    return _json({ status: 'error', message: 'invalid JSON body' });
  }

  var secret = PropertiesService.getScriptProperties().getProperty('DISPATCH_SECRET');
  if (!secret || payload.secret !== secret) {
    return _json({ status: 'error', message: 'unauthorized' });
  }

  if (payload.mode === 'health') {
    return _json({
      status: 'ok',
      account: Session.getEffectiveUser().getEmail(),
      quotaRemaining: MailApp.getRemainingDailyQuota(),
      version: VERSION,
    });
  }

  if (payload.mode !== 'send' && payload.mode !== 'draft') {
    return _json({ status: 'error', message: 'mode must be send|draft|health' });
  }

  var messages = payload.messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    return _json({ status: 'error', message: 'messages[] is required' });
  }
  if (messages.length > MAX_MESSAGES_PER_CALL) {
    return _json({
      status: 'error',
      message: 'max ' + MAX_MESSAGES_PER_CALL + ' messages per call',
    });
  }

  var results = [];
  var okCount = 0;
  for (var i = 0; i < messages.length; i++) {
    var m = messages[i];
    try {
      _validateMessage(m);
      if (MailApp.getRemainingDailyQuota() <= 0) {
        throw new Error('QUOTA_EXHAUSTED');
      }
      var options = _buildOptions(m);
      if (payload.mode === 'draft') {
        GmailApp.createDraft(m.to, m.subject, m.plainText || '', options);
      } else {
        GmailApp.sendEmail(m.to, m.subject, m.plainText || '', options);
      }
      okCount++;
      results.push({ to: m.to, ok: true });
      Utilities.sleep(100); // pacing, matches legacy INDIVIDUAL mode
    } catch (err) {
      results.push({ to: (m && m.to) || '(missing)', ok: false, error: String(err.message || err) });
    }
  }

  return _json({
    status: okCount === messages.length ? 'ok' : (okCount > 0 ? 'partial' : 'error'),
    quotaRemaining: MailApp.getRemainingDailyQuota(),
    results: results,
  });
}

/** @private — required-field validation; throws on failure. */
function _validateMessage(m) {
  if (!m || typeof m !== 'object') throw new Error('message must be an object');
  var emailRe = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
  if (!m.to || !emailRe.test(String(m.to).trim())) {
    throw new Error('invalid "to": ' + m.to);
  }
  if (!m.subject || !String(m.subject).trim()) throw new Error('missing subject');
  if (!m.htmlBody || !String(m.htmlBody).trim()) throw new Error('missing htmlBody');
}

/** @private — assembles GmailApp options incl. resolved attachments. */
function _buildOptions(m) {
  var options = { htmlBody: m.htmlBody };
  if (m.cc) options.cc = m.cc;
  if (m.bcc) options.bcc = m.bcc;
  if (m.replyTo) options.replyTo = m.replyTo;
  if (m.senderName) options.name = m.senderName;
  var ids = m.attachmentFileIds;
  if (Array.isArray(ids) && ids.length > 0) {
    options.attachments = _resolveAttachments(ids);
  }
  return options;
}

/**
 * @private — resolves Drive IDs to blobs.
 * A single folder ID expands to its direct children (subfolders ignored);
 * Google-native files export to PDF and are renamed "<name>.pdf".
 * Throws ATTACH_NOT_FOUND / ATTACH_TOO_LARGE (exact legacy strings).
 */
function _resolveAttachments(ids) {
  var fileIds = _expandSingleFolderId(ids);
  var blobs = [];
  var totalBytes = 0;
  for (var i = 0; i < fileIds.length; i++) {
    var id = String(fileIds[i]).trim();
    if (!id) continue;
    var file;
    try {
      file = DriveApp.getFileById(id);
    } catch (err) {
      throw new Error('ATTACH_NOT_FOUND id=' + id);
    }
    var blob;
    if (file.getMimeType().indexOf('application/vnd.google-apps.') === 0) {
      blob = file.getAs('application/pdf').setName(file.getName() + '.pdf');
    } else {
      blob = file.getBlob();
    }
    totalBytes += blob.getBytes().length;
    if (totalBytes > MAX_ATTACH_BYTES) {
      throw new Error('ATTACH_TOO_LARGE total=' + totalBytes);
    }
    blobs.push(blob);
  }
  return blobs;
}

/** @private — if ids is exactly one folder ID, expand to its direct children. */
function _expandSingleFolderId(ids) {
  var cleaned = [];
  for (var i = 0; i < ids.length; i++) {
    var id = String(ids[i]).trim();
    if (id) cleaned.push(id);
  }
  if (cleaned.length !== 1) return cleaned;
  try {
    var folder = DriveApp.getFolderById(cleaned[0]);
    var out = [];
    var it = folder.getFiles();
    while (it.hasNext()) out.push(it.next().getId());
    return out; // empty folder → no attachments, caller sends without
  } catch (err) {
    return cleaned; // not a folder — treat as a file ID
  }
}

/** @private — JSON response helper. */
function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
