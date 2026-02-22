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

// ── Blob resolution ───────────────────────────────────────────────────────────

/**
 * Resolves an array of Drive file IDs to Blob objects, exporting Google-native
 * files to PDF when requested.
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

  ids.forEach(id => {
    try {
      const f    = DriveApp.getFileById(id);
      const mime = f.getMimeType();
      let blob;

      if (opts.exportGoogleToPdf && /^application\/vnd\.google\-apps\./.test(mime)) {
        // Requires Advanced Drive service
        const exported = Drive.Files.export(id, 'application/pdf');
        blob = Utilities.newBlob(exported.getBytes(), 'application/pdf', f.getName() + '.pdf');
      } else {
        blob = f.getBlob();
      }

      total += blob.getBytes().length;
      blobs.push(blob);
    } catch (_) {
      throw new Error(`ATTACH_NOT_FOUND id=${id}`);
    }
  });

  if (total > maxBytes) throw new Error(`ATTACH_TOO_LARGE total=${total}`);
  return blobs;
}

// ── Name summary (dry-run only) ───────────────────────────────────────────────

/**
 * Resolves file names from Drive IDs without downloading blobs.
 * Used in dry-run previews to list attachment names without triggering exports.
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
      const f    = DriveApp.getFileById(id);
      const mime = f.getMimeType();
      const isGoogle = /^application\/vnd\.google\-apps\./.test(mime);
      names.push(f.getName() + (isGoogle ? ' (Google file → PDF at send)' : ''));
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
