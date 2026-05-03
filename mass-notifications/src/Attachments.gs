/**
 * Mass Notifications — Attachments
 * Drive file resolution, size enforcement, PDF export, and pre-flight validation.
 *
 * Rules (mirrors original behaviour):
 *   • Gmail hard limit ~25 MB; we enforce MAX_ATTACH_BYTES (~20 MB) per message.
 *   • Google Docs/Sheets/Slides are auto-exported to PDF at send time.
 *     (Requires Advanced Drive API: Services → + → Drive API)
 *   • INDIVIDUAL mode: config attachments + per-row attachments, merged.
 *   • BCC mode: config-level attachments only.
 *   • Any unresolvable file → row marked REVIEW with note "ATTACH_NOT_FOUND id=…"
 *   • Total size > limit   → row marked REVIEW with note "ATTACH_TOO_LARGE total=…"
 */

const MAX_ATTACH_BYTES = 20 * 1024 * 1024; // 20 MB

// ── ID resolution (files + folders) ───────────────────────────────────────────

const DRIVE_FOLDER_MIME = 'application/vnd.google-apps.folder';

/**
 * Resolves a single Drive ID to one or more Files. If the ID is a folder,
 * returns its direct children (subfolders are ignored — Move-In flow uses
 * a flat folder of attachments per reservation).
 *
 * Throws if the ID is unreadable or doesn't exist.
 *
 * @param {string} id
 * @return {GoogleAppsScript.Drive.File[]}
 */
function expandSingleIdToFiles_(id) {
  // Advanced Drive (v2) gives the cleanest type check without a try/catch
  // dance between getFileById/getFolderById.
  const meta = Drive.Files.get(id);

  if (meta.mimeType === DRIVE_FOLDER_MIME) {
    const folder = DriveApp.getFolderById(id);
    const out    = [];
    const it     = folder.getFiles();
    while (it.hasNext()) out.push(it.next());
    return out;
  }

  return [DriveApp.getFileById(id)];
}

/**
 * Resolves an array of Drive IDs (files OR folders) to a flat list of Files.
 * Wraps any underlying error as ATTACH_NOT_FOUND so callers can surface a
 * row-level review status without leaking Drive API internals.
 *
 * @param {string[]} ids
 * @return {GoogleAppsScript.Drive.File[]}
 * @throws {Error} ATTACH_NOT_FOUND
 */
function expandIdsToFiles_(ids) {
  const files = [];
  ids.forEach(id => {
    try {
      expandSingleIdToFiles_(id).forEach(f => files.push(f));
    } catch (_) {
      throw new Error(`ATTACH_NOT_FOUND id=${id}`);
    }
  });
  return files;
}

// ── Blob resolution ───────────────────────────────────────────────────────────

/**
 * Resolves an array of Drive file/folder IDs to Blob objects, exporting
 * Google-native files to PDF when requested. Folder IDs expand to their
 * direct children before blob conversion.
 *
 * @param {string[]} ids
 * @param {{ exportGoogleToPdf?: boolean, maxBytes?: number }} opts
 * @return {Blob[]}
 * @throws {Error} ATTACH_NOT_FOUND or ATTACH_TOO_LARGE
 */
function getBlobsForIds_(ids, opts = {}) {
  const maxBytes = opts.maxBytes || MAX_ATTACH_BYTES;
  const blobs    = [];
  let   total    = 0;

  const files = expandIdsToFiles_(ids);

  files.forEach(f => {
    const mime = f.getMimeType();
    let blob;

    if (opts.exportGoogleToPdf && /^application\/vnd\.google\-apps\./.test(mime)) {
      // Requires Advanced Drive service.
      const exported = Drive.Files.export(f.getId(), 'application/pdf');
      blob = Utilities.newBlob(exported.getBytes(), 'application/pdf', f.getName() + '.pdf');
    } else {
      blob = f.getBlob();
    }

    total += blob.getBytes().length;
    blobs.push(blob);
  });

  if (total > maxBytes) throw new Error(`ATTACH_TOO_LARGE total=${total}`);
  return blobs;
}

// ── Name summary (dry-run only) ───────────────────────────────────────────────

/**
 * Resolves file names from Drive IDs without downloading blobs.
 * Folder IDs expand to their direct children. Used in dry-run previews
 * so the operator can sanity-check what's about to attach.
 *
 * @param {string[]} ids
 * @return {{ names: string[], notes: string[] }}
 */
function summarizeAttachmentNames_(ids) {
  if (!ids || !ids.length) return { names: [], notes: [] };
  const names = [];
  const notes = [];

  ids.forEach(id => {
    try {
      const meta     = Drive.Files.get(id);
      const isFolder = meta.mimeType === DRIVE_FOLDER_MIME;
      const files    = expandSingleIdToFiles_(id);

      if (isFolder && !files.length) {
        notes.push(`Folder is empty: id=${id}`);
        return;
      }

      // v2 Drive uses `title` for the human-readable name.
      const folderLabel = isFolder ? ` — from folder "${meta.title || id}"` : '';
      files.forEach(f => {
        const isGoogle = /^application\/vnd\.google\-apps\./.test(f.getMimeType());
        names.push(f.getName() + (isGoogle ? ' (Google file → PDF at send)' : '') + folderLabel);
      });
    } catch (_) {
      notes.push(`ATTACH_NOT_FOUND id=${id}`);
    }
  });

  return { names, notes };
}

// ── Pre-flight validation ─────────────────────────────────────────────────────

/**
 * Menu entry: validates all attachments for eligible rows before sending.
 * Shows a modal summary of any issues found.
 */
function validateAttachments_() {
  const cfg    = loadConfig_();
  const sh     = getRecipientsSheet_(cfg);
  const last   = sh.getLastRow();
  const cfgIds = parseIdList_(cfg.attachmentFileIds);
  const opts   = { exportGoogleToPdf: true, maxBytes: MAX_ATTACH_BYTES };

  let ok = 0, review = 0, rowsChecked = 0;
  const issues = [];

  // Config-level files first
  try {
    if (cfgIds.length) getBlobsForIds_(cfgIds, opts);
  } catch (e) {
    issues.push(`Config attachments failed: ${e.message}`);
  }

  if (last >= 2) {
    const data = sh.getRange(2, 1, last - 1, COL_MAX).getValues();
    data.forEach((r, i) => {
      const status = String(r[COL.STATUS - 1] || '').toUpperCase();
      if (status && status !== 'PENDING' && status !== 'READY') return;

      const rowIds = parseIdList_(r[COL.ATTACH_IDS - 1]);
      const allIds = [...cfgIds, ...rowIds];
      if (!allIds.length) { ok++; rowsChecked++; return; }

      try {
        getBlobsForIds_(allIds, opts);
        ok++;
      } catch (e) {
        review++;
        issues.push(`Row ${i + 2}: ${e.message}`);
      }
      rowsChecked++;
    });
  }

  const html = `
    <div style="font-family:Arial,Helvetica,sans-serif;padding:10px;">
      <h2 style="margin:0 0 8px;">Attachments Validation</h2>
      <div><strong>Rows checked:</strong> ${rowsChecked}</div>
      <div><strong>OK:</strong> ${ok}</div>
      <div><strong>Need review:</strong> ${review}</div>
      ${issues.length
        ? '<hr><div><strong>Issues:</strong><br>' +
          issues.map(x => escapeHtml_(x)).join('<br>') + '</div>'
        : ''}
    </div>`;

  showHtmlOrDraft_(html, 'Attachments Validation');
}
