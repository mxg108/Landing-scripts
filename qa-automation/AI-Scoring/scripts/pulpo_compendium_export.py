#!/usr/bin/env python3
"""Pulpo SOP Compendium — full-corpus snapshot → Google Doc.

Read-only against Pulpo (benchmark token by default — separate quota
pool, never starves production scoring). Three stages, each skippable:

  extract   enumerate the ENTIRE corpus (union of trending-90d, tag
            transitive closure, and a semantic sweep looped until
            twice-dry), then one raw get_document per id. Raw payloads
            are preserved — created_at/owner/review fields included.
  build     corpus JSON → compendium HTML: clickable index (the ONE
            anchor form Drive's importer converts to real bookmarks is
            <a name>), tag map, untagged section, full contents with
            metadata tables, open-flag callouts, Mermaid flowcharts.
  deliver   Drive files.update with HTML→Doc conversion into an
            EXISTING Google Doc (service accounts have zero Drive
            storage quota — they can never files.create; the target
            doc must be user-owned, shared to the SA as editor), then
            a docx-export verification that every internal link
            resolves to a real bookmark.

Also computes a change résumé vs a previous corpus snapshot (--prev):
added / removed / updated docs, flag and status deltas — the weekly
job's chiclet body.

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/pulpo_compendium_export.py                # full run
        [--doc-id …]          target Google Doc (default:
                              $PULPO_COMPENDIUM_DOC_ID)
        [--out-dir …]         artifacts dir (default: scripts/.compendium)
        [--corpus path.json]  skip extract, reuse a snapshot
        [--prev path.json]    previous snapshot for the change résumé
        [--skip-deliver]      stop after build (writes HTML only)
        [--token-env NAME]    default PULPO_MCP_BENCHMARK_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import html as htmlmod
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AI_SCORING))
load_dotenv(_AI_SCORING / ".env")

GENERATED_TZ = "America/Mexico_City"

# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

SEED_TAGS = [
    "nmt", "ota", "verifications", "sofia", "member_support",
    "coach-card", "onboarding", "sales", "qa", "billing", "payments",
    "maintenance", "cleaning", "standby", "transfers", "renewals",
    "engineering", "troubleshooting", "how-to", "policies",
]

TOPIC_PROBES = [
    "standby cancellation policy and refunds",
    "security deposit and damages",
    "member verification steps",
    "reservation extension and renewal",
    "OTA booking change or cancellation",
    "maintenance request escalation",
    "cleaning and housekeeping standards",
    "payment failure and collections",
    "move-in and move-out process",
    "transfer to another unit",
    "lease and membership terms",
    "pricing discounts and promotions",
    "escalation to supervisor",
    "pet policy",
    "parking policy",
    "wifi and utilities setup",
    "lockout and key access",
    "furniture and appliance issues",
    "pest control",
    "noise complaint handling",
    "refund processing timeline",
    "chargeback dispute",
    "identity fraud check",
    "background check requirements",
    "corporate housing accounts",
    "guest policy",
    "early termination fees",
    "billing cycle and invoices",
    "app troubleshooting",
    "is Landing legit",
    "customer trust and safety",
    "emergency after hours support",
    "sales pitch and objection handling",
    "tour scheduling",
    "waitlist process",
    "referral program",
    "credit and promo codes",
    "insurance requirements",
    "smoking policy violation",
    "package delivery and mail",
]

RATE_LIMIT_WAIT_S = 65
PACE_EVERY = 10          # spend ~10 rate units, then pause
PACE_SLEEP_S = 12
HARVEST_STOP_WORDS = {"landing", "with", "from", "your", "this", "that",
                      "sops", "what"}


class _Pacer:
    """60/min per token, and the limiter rejects bursts exceeding the
    REMAINING minute allowance (observed live 2026-07-22): small spends
    with pauses beat big bursts."""

    def __init__(self):
        self._since_pause = 0
        self.total = 0

    async def spend(self, units: int):
        self._since_pause += units
        self.total += units
        if self._since_pause >= PACE_EVERY:
            await asyncio.sleep(PACE_SLEEP_S)
            self._since_pause = 0


async def _call(provider, pacer, tool, args, units=1):
    await pacer.spend(units)
    try:
        return await provider.raw_tool_call(tool, args)
    except Exception as e:
        if "rate_limited" not in str(e):
            raise
        print(f"  rate-limited on {tool} — waiting {RATE_LIMIT_WAIT_S}s", flush=True)
        await asyncio.sleep(RATE_LIMIT_WAIT_S)
        return await provider.raw_tool_call(tool, args)


def _norm_results(payload):
    if not isinstance(payload, dict):
        return []
    if "documents" in payload:
        return payload.get("documents") or []
    if "results" in payload:
        return payload.get("results") or []
    if "batches" in payload:
        out = []
        for b in payload.get("batches") or []:
            out.extend(b.get("results") or [])
        return out
    return []


async def _disposition_labels() -> list[str]:
    """Same source as the coverage probe; non-fatal fallback to []."""
    try:
        import asyncpg
        conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=10)
        try:
            rows = await conn.fetch(
                """
                SELECT DISTINCT disposition_category, disposition
                FROM command_center.calls
                WHERE disposition_category IS NOT NULL
                """
            )
        finally:
            await conn.close()
        return [f"{r['disposition_category']} — {r['disposition']}"
                if r["disposition"] else r["disposition_category"]
                for r in rows]
    except Exception as e:
        print(f"  disposition labels unavailable ({e!r:.80}) — sweep "
              f"proceeds without them", flush=True)
        return []


async def extract_corpus(provider) -> dict:
    """Enumerate + fetch the whole corpus. Returns the snapshot dict
    (raw listing + raw get_document payload per doc)."""
    pacer = _Pacer()
    docs: dict[str, dict] = {}
    sources: dict[str, list] = {}
    tags_seen: set[str] = set(SEED_TAGS)
    tags_listed: set[str] = set()
    queries_sent: set[str] = set()

    def absorb(results, source):
        new = 0
        for r in results:
            rid = str(r.get("id", ""))
            if not rid:
                continue
            if rid not in docs:
                docs[rid] = dict(r)
                sources[rid] = []
                new += 1
            else:
                for k, v in r.items():
                    if v not in (None, "", [], 0) and not docs[rid].get(k):
                        docs[rid][k] = v
            if source not in sources[rid]:
                sources[rid].append(source)
            for t in (r.get("tags") or []):
                tags_seen.add(str(t).lower())
        return new

    async def list_pending_tags():
        added = 0
        while True:
            pending = sorted(tags_seen - tags_listed)
            if not pending:
                return added
            for tag in pending:
                tags_listed.add(tag)
                payload = await _call(provider, pacer, "list_documents_by_tag",
                                      {"tags": [tag], "limit": 50})
                results = _norm_results(payload)
                if len(results) == 50:
                    print(f"  WARNING tag {tag!r} returned exactly 50 — "
                          f"possible truncation", flush=True)
                added += absorb(results, f"tag:{tag}")

    async def sweep(queries, round_name):
        fresh = [q[:500] for q in queries if q and q not in queries_sent]
        added = 0
        for i in range(0, len(fresh), 10):
            chunk = fresh[i:i + 10]
            queries_sent.update(chunk)
            payload = await _call(provider, pacer, "search_knowledge_base",
                                  {"queries": chunk, "limit": 20, "rerank": False},
                                  units=len(chunk))
            for b in (payload.get("batches") or []) if isinstance(payload, dict) else []:
                added += absorb(b.get("results") or [],
                                f"search:{b.get('query', '?')[:60]}")
        print(f"  sweep {round_name!r}: {len(fresh)} queries, +{added} new "
              f"(total {len(docs)})", flush=True)
        return added

    print("extract: trending (90d)", flush=True)
    payload = await _call(provider, pacer, "get_trending_documents",
                          {"window_days": 90, "limit": 50})
    absorb(_norm_results(payload), "trending:90d")

    print("extract: tag closure", flush=True)
    await list_pending_tags()

    print("extract: search sweep", flush=True)
    await sweep(await _disposition_labels(), "dispositions")
    await sweep(TOPIC_PROBES, "topic probes")
    await sweep([t.replace("-", " ").replace("_", " ")
                 for t in sorted(tags_seen)], "tag names")

    dry, round_no = 0, 0
    while dry < 2:
        round_no += 1
        words = {w.lower()
                 for d in docs.values()
                 for w in re.findall(r"[A-Za-z]{4,}", d.get("title", ""))}
        harvest = sorted(w for w in words
                         if w not in HARVEST_STOP_WORDS and w not in queries_sent)
        if not harvest:
            break
        added = await sweep(harvest[:60], f"harvest-{round_no}")
        added += await list_pending_tags()
        dry = dry + 1 if added == 0 else 0

    print(f"extract: get_document x {len(docs)}", flush=True)
    full: dict[str, dict] = {}
    for i, rid in enumerate(sorted(docs)):
        payload = await _call(provider, pacer, "get_document", {"id": rid})
        raw = (payload.get("document")
               if isinstance(payload, dict) and isinstance(payload.get("document"), dict)
               else payload)
        if isinstance(raw, dict) and raw.get("id"):
            full[rid] = raw
        else:
            print(f"  WARNING get_document({rid}) → unusable payload", flush=True)
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(docs)}", flush=True)

    return {
        "generated_note": "raw Pulpo MCP payloads; listing metadata under "
                          "'listing', full get_document under 'document'",
        "stats": {
            "unique_docs": len(docs),
            "fetched": len(full),
            "tags_listed": sorted(tags_listed),
            "queries_sent": len(queries_sent),
            "rate_units_spent": pacer.total,
        },
        "docs": [{"id": rid, "listing": docs[rid], "sources": sources[rid],
                  "document": full.get(rid)}
                 for rid in sorted(docs)],
    }


# ---------------------------------------------------------------------------
# diff (the weekly chiclet's résumé)
# ---------------------------------------------------------------------------

def merged_entries(corpus: dict) -> list[dict]:
    """listing ∪ document (document wins), sorted by title."""
    entries = []
    for row in corpus["docs"]:
        listing = row.get("listing") or {}
        doc = row.get("document") or {}
        merged = {**listing,
                  **{k: v for k, v in doc.items() if v not in (None, "", [], {})}}
        if merged.get("id"):
            entries.append(merged)
    entries.sort(key=lambda d: (d.get("title") or "").lower())
    return entries


def diff_corpora(prev: dict, curr: dict) -> dict:
    """Change résumé between two snapshots: what a manager should skim."""
    old = {d["id"]: d for d in merged_entries(prev)}
    new = {d["id"]: d for d in merged_entries(curr)}
    added = [new[i]["title"] for i in new if i not in old]
    removed = [old[i]["title"] for i in old if i not in new]
    updated, flag_changes = [], []
    for i in set(old) & set(new):
        if (new[i].get("updated_at") or "") != (old[i].get("updated_at") or ""):
            updated.append(new[i]["title"])
        old_flags = len(old[i].get("open_flags") or [])
        new_flags = len(new[i].get("open_flags") or [])
        if new_flags != old_flags:
            flag_changes.append(
                f"{new[i]['title']} ({old_flags}→{new_flags} open flags)")
    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "updated": sorted(updated),
        "flag_changes": sorted(flag_changes),
        "total_docs": len(new),
    }


def diff_resume(diff: dict, limit: int = 3) -> str:
    """One-liner résumé for the toast/chiclet body."""

    def clip(names):
        head = ", ".join(names[:limit])
        more = len(names) - limit
        return head + (f" +{more} more" if more > 0 else "")

    parts = []
    if diff["added"]:
        parts.append(f"{len(diff['added'])} new: {clip(diff['added'])}")
    if diff["updated"]:
        parts.append(f"{len(diff['updated'])} updated: {clip(diff['updated'])}")
    if diff["removed"]:
        parts.append(f"{len(diff['removed'])} removed: {clip(diff['removed'])}")
    if diff["flag_changes"]:
        parts.append(f"flags: {clip(diff['flag_changes'])}")
    if not parts:
        parts.append("no changes")
    return f"{diff['total_docs']} docs — " + "; ".join(parts)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

MD_EXT = ["tables", "fenced_code", "sane_lists"]


def _esc(s):
    return htmlmod.escape(str(s or ""), quote=True)


def _date(v):
    s = str(v or "").strip()
    if not s:
        return "—"
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ]?(\d{2}:\d{2})?", s)
    if m:
        return m.group(1) + (f" {m.group(2)}" if m.group(2) else "")
    return s[:32]


def build_html(corpus: dict, generated_on: str) -> str:
    import markdown

    entries = merged_entries(corpus)
    tagged = [d for d in entries if d.get("tags")]
    untagged = [d for d in entries if not d.get("tags")]
    open_flagged = [d for d in entries if d.get("open_flags")]
    flowcharts = [d for d in entries if d.get("body_format") == "flowchart"]
    status_flagged = [d for d in entries if d.get("status") == "flagged"]

    tag_map: dict[str, list] = {}
    for d in entries:
        for t in (d.get("tags") or []):
            tag_map.setdefault(str(t).lower(), []).append(d)

    anchor = {d["id"]: f"doc-{i+1}" for i, d in enumerate(entries)}

    p = ["<h1>Pulpo SOP Compendium</h1>"]
    p.append(
        f"<p><b>Generated {generated_on}</b> from the live Pulpo knowledge "
        f"base (plasticity.heypulpo.com) over MCP, using the QA benchmark "
        f"token (both audiences, unassigned — the reproducible QA view: "
        f"every published doc, no person-private docs).</p>")
    p.append("<table border='1' cellpadding='4'>")
    p.append(f"<tr><td>Total documents</td><td>{len(entries)}</td></tr>")
    p.append(f"<tr><td>Tagged / untagged</td><td>{len(tagged)} / {len(untagged)}</td></tr>")
    p.append(f"<tr><td>Distinct tags</td><td>{len(tag_map)}</td></tr>")
    p.append(f"<tr><td>Verification status</td><td>"
             f"{sum(1 for d in entries if d.get('status') == 'verified')} verified, "
             f"{len(status_flagged)} flagged</td></tr>")
    p.append(f"<tr><td>Docs with open review flags</td><td>{len(open_flagged)}</td></tr>")
    p.append(f"<tr><td>Flowchart (Mermaid) documents</td><td>{len(flowcharts)}</td></tr>")
    p.append(f"<tr><td>Audiences</td><td>"
             f"{sum(1 for d in entries if 'customer' in (d.get('audiences') or []))} "
             f"customer-readable, rest internal-only</td></tr>")
    p.append("</table>")
    p.append(
        "<p><i>Method: union of trending-documents (90-day window), full tag "
        "transitive closure, and a semantic search sweep looped until twice "
        "dry. Contents are verbatim document bodies from get_document. "
        "Timestamps are as exposed by the platform (UTC).</i></p>")

    p.append("<h1>Index</h1>")
    p.append("<table border='1' cellpadding='4'>")
    p.append("<tr><th>#</th><th>Title</th><th>Tags</th><th>Created</th>"
             "<th>Updated</th><th>⚑</th></tr>")
    for i, d in enumerate(entries):
        marks = []
        if d.get("open_flags"):
            marks.append(f"⚑{len(d['open_flags'])}")
        if d.get("status") == "flagged":
            marks.append("status:flagged")
        p.append(
            f"<tr><td>{i+1}</td>"
            f"<td><a href='#{anchor[d['id']]}'>{_esc(d.get('title'))}</a></td>"
            f"<td>{_esc(', '.join(d.get('tags') or []) or '—')}</td>"
            f"<td>{_date(d.get('created_at'))}</td>"
            f"<td>{_date(d.get('updated_at'))}</td>"
            f"<td>{' '.join(marks)}</td></tr>")
    p.append("</table>")

    p.append("<h2>Documents by tag</h2>")
    for t in sorted(tag_map):
        links = " · ".join(f"<a href='#{anchor[d['id']]}'>{_esc(d.get('title'))}</a>"
                           for d in tag_map[t])
        p.append(f"<p><b>{_esc(t)}</b> ({len(tag_map[t])}): {links}</p>")
    if untagged:
        links = " · ".join(f"<a href='#{anchor[d['id']]}'>{_esc(d.get('title'))}</a>"
                           for d in untagged)
        p.append(f"<p><b>UNTAGGED</b> ({len(untagged)}): {links}</p>")

    p.append("<h1>Contents</h1>")
    for i, d in enumerate(entries):
        # <a name> is the ONE anchor form Drive's HTML importer turns into
        # a real Docs bookmark (id= variants convert to dead links).
        p.append(f"<h2><a name='{anchor[d['id']]}'></a>{i+1}. "
                 f"{_esc(d.get('title'))}</h2>")
        p.append("<table border='1' cellpadding='4'>")
        p.append(f"<tr><td>Tags</td><td>"
                 f"{_esc(', '.join(d.get('tags') or []) or '(untagged)')}</td></tr>")
        p.append(f"<tr><td>Created</td><td>{_date(d.get('created_at'))}</td></tr>")
        p.append(f"<tr><td>Last updated</td><td>{_date(d.get('updated_at'))}</td></tr>")
        cad = d.get("review_cadence") or ""
        p.append(f"<tr><td>Verification</td><td>status: "
                 f"{_esc(d.get('status') or '?')} · last verified "
                 f"{_date(d.get('last_verified_at'))} · next review "
                 f"{_date(d.get('next_review_at'))}"
                 f"{' (' + _esc(cad) + ')' if cad else ''}</td></tr>")
        if d.get("owner_name"):
            p.append(f"<tr><td>Owner</td><td>{_esc(d['owner_name'])}</td></tr>")
        p.append(f"<tr><td>Audiences</td><td>"
                 f"{_esc(', '.join(d.get('audiences') or []) or 'internal')}</td></tr>")
        if d.get("body_format") == "flowchart":
            p.append("<tr><td>Format</td><td>flowchart (Mermaid)</td></tr>")
        if d.get("url"):
            p.append(f"<tr><td>Pulpo link</td><td><a href='{_esc(d['url'])}'>"
                     f"{_esc(d['url'])}</a></td></tr>")
        p.append(f"<tr><td>Document id</td><td>{_esc(d['id'])}</td></tr>")
        p.append("</table>")

        if d.get("summary"):
            p.append(f"<p><i><b>Summary:</b> {_esc(d['summary'])}</i></p>")

        open_flags = d.get("open_flags") or []
        if open_flags:
            p.append(f"<p><b>⚑ {len(open_flags)} open review flag(s) — "
                     "verify flagged passages before acting:</b></p>")
            for f in open_flags:
                line = f"<blockquote>“{_esc(f.get('quote'))}”"
                if f.get("body"):
                    line += f"<br><b>Concern:</b> {_esc(f['body'])}"
                if f.get("suggestion"):
                    line += f"<br><b>Suggested fix:</b> {_esc(f['suggestion'])}"
                p.append(line + "</blockquote>")

        body = d.get("body") or ""
        if d.get("body_format") == "flowchart":
            p.append("<p><i>Process flowchart stored as Mermaid source — read "
                     "as a decision graph. %% def lines carry each node's full "
                     "rule; %% always / %% never lines are doc-wide "
                     "guardrails.</i></p>")
            p.append(f"<pre>{_esc(body)}</pre>")
        elif body:
            p.append(markdown.markdown(body, extensions=MD_EXT))
        else:
            p.append("<p><i>(empty body)</i></p>")
        p.append("<hr>")

    return ("<html><head><meta charset='utf-8'>"
            "<title>Pulpo SOP Compendium</title></head><body>"
            + "\n".join(p) + "</body></html>")


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------

def _sa_headers() -> dict:
    import google.auth.transport.requests
    from google.oauth2.service_account import Credentials

    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    scopes = ["https://www.googleapis.com/auth/drive"]
    if raw.strip().startswith("{"):
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(raw, scopes=scopes)
    creds.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {creds.token}"}


def deliver_html(doc_id: str, html: str) -> dict:
    """Replace the target Google Doc's content with `html` (Drive
    update-with-conversion), then verify via docx export that every
    internal link resolves to a real bookmark. Returns a report dict;
    raises RuntimeError on hard failure."""
    import httpx

    headers = _sa_headers()
    r = httpx.get(f"https://www.googleapis.com/drive/v3/files/{doc_id}",
                  params={"supportsAllDrives": "true",
                          "fields": "id,name,mimeType,capabilities(canModifyContent)"},
                  headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"files.get failed HTTP {r.status_code}: {r.text[:200]}")
    meta = r.json()
    if meta["mimeType"] != "application/vnd.google-apps.document":
        raise RuntimeError(f"target {doc_id} is not a Google Doc")
    if not meta.get("capabilities", {}).get("canModifyContent"):
        raise RuntimeError("service account cannot modify this doc")

    boundary = "B0UNDARYpulpo"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps({"mimeType": "application/vnd.google-apps.document"})
        + f"\r\n--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n"
    ).encode() + html.encode() + f"\r\n--{boundary}--".encode()
    r = httpx.patch(
        f"https://www.googleapis.com/upload/drive/v3/files/{doc_id}",
        params={"uploadType": "multipart", "supportsAllDrives": "true"},
        headers={**headers,
                 "Content-Type": f"multipart/related; boundary={boundary}"},
        content=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"update failed HTTP {r.status_code}: {r.text[:300]}")

    # Verify via docx export — the HTML export omits bookmark elements,
    # so bookmarks vs hyperlink anchors in the OOXML is the ground truth.
    r = httpx.get(
        f"https://www.googleapis.com/drive/v3/files/{doc_id}/export",
        params={"mimeType": "application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document"},
        headers=headers, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"verify export failed HTTP {r.status_code}")
    xml = zipfile.ZipFile(io.BytesIO(r.content)).read("word/document.xml").decode()
    bookmarks = set(re.findall(r'w:bookmarkStart[^>]*w:name="([^"]+)"', xml))
    anchors = re.findall(r'w:hyperlink[^>]*w:anchor="([^"]+)"', xml)
    resolved = sum(1 for a in anchors if a in bookmarks)
    expected = len(set(re.findall(r"<a name='(doc-[^']*)'", html)))
    report = {
        "doc_name": meta["name"],
        "sections": expected,
        "bookmarks": len(bookmarks),
        "internal_links": len(anchors),
        "resolved": resolved,
        "links_ok": bool(anchors) and resolved == len(anchors)
                    and len(bookmarks) >= expected,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
    }
    if not report["links_ok"]:
        raise RuntimeError(f"internal-link verification failed: {report}")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _amain() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--doc-id", default=os.environ.get("PULPO_COMPENDIUM_DOC_ID", ""))
    ap.add_argument("--out-dir", default=str(_AI_SCORING / "scripts" / ".compendium"))
    ap.add_argument("--corpus", default=None,
                    help="reuse an existing snapshot JSON (skip extract)")
    ap.add_argument("--prev", default=None,
                    help="previous snapshot JSON for the change résumé")
    ap.add_argument("--skip-deliver", action="store_true")
    ap.add_argument("--token-env", default="PULPO_MCP_BENCHMARK_TOKEN")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(GENERATED_TZ))
    stamp = now.strftime("%Y-%m-%d")

    if args.corpus:
        corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
        print(f"corpus: reused {args.corpus} "
              f"({corpus['stats']['fetched']} docs)")
    else:
        from backend.services.rag.pulpo import build_from_env
        provider = build_from_env(args.token_env)
        if provider is None:
            print(f"ERROR: PULPO_MCP_URL / {args.token_env} not set")
            return 1
        try:
            corpus = await extract_corpus(provider)
        finally:
            await provider.aclose()
        corpus_path = out_dir / f"pulpo_corpus_{stamp}.json"
        corpus_path.write_text(json.dumps(corpus, indent=1, ensure_ascii=False),
                               encoding="utf-8")
        print(f"corpus: {corpus['stats']['fetched']} docs, "
              f"{corpus['stats']['rate_units_spent']} rate units → {corpus_path}")

    if args.prev:
        prev = json.loads(Path(args.prev).read_text(encoding="utf-8"))
        resume = diff_resume(diff_corpora(prev, corpus))
        print(f"changes vs previous: {resume}")

    html = build_html(corpus, generated_on=now.strftime("%Y-%m-%d %H:%M %Z"))
    html_path = out_dir / f"compendium_{stamp}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"html: {len(html)} chars → {html_path}")
    if len(html) > 900_000:
        print("WARNING: nearing Google Docs' ~1.02M char ceiling")

    if args.skip_deliver:
        return 0
    if not args.doc_id:
        print("ERROR: no --doc-id and PULPO_COMPENDIUM_DOC_ID unset")
        return 1
    report = deliver_html(args.doc_id, html)
    print(f"delivered to {report['doc_name']!r}: "
          f"{report['resolved']}/{report['internal_links']} links resolve "
          f"across {report['bookmarks']} bookmarks")
    print(f"doc url: {report['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
