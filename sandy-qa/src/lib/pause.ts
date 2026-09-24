// Auto-scoring pause switch (migration 0022; owner instruction 2026-09-24:
// "nothing moves until Diana or Todd take over this project").
//
// One row in qa_settings (key 'autoscore_paused') gates every AUTOMATIC
// scoring path — the nightly disposition sweep, the Retell sweep and the
// one-shot cron jobs (EOM assessments, HR-bonus dispatch, daily digest).
// Human-driven scoring (console "Score Call", rescore) and the non-scoring
// jobs (EOD sheet report, supervisor pulls, queue pump) are NOT gated.
//
// Resume: DELETE FROM qa_settings WHERE key = 'autoscore_paused'  (via
// `sandy.py db migrate`); no deploy needed. Pause again: re-insert the row
// (see migrations/0022_autoscore_pause.sql).

export interface AutoscorePause {
  since?: string;
  by?: string;
  reason?: string;
}

export async function getAutoscorePause(db: D1Database): Promise<AutoscorePause | null> {
  try {
    const row = await db
      .prepare("SELECT value FROM qa_settings WHERE key = 'autoscore_paused'")
      .first<{ value: string | null }>();
    if (!row) return null;
    try {
      return row.value ? (JSON.parse(row.value) as AutoscorePause) : {};
    } catch {
      return {};
    }
  } catch {
    // table absent (pre-0022 database) → not paused
    return null;
  }
}
