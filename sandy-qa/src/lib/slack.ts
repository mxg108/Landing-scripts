// Slack seam (ShiftReport.md §1.3 — "NOT built yet" until now; first
// consumer is SupervisorDeliverables.md §6). Three calls, plain fetch:
//   postMessage      — bot token, Block Kit; returns the ts (the re-post gate)
//   searchMentions   — user token, search.messages for subteam mentions
//   messageMeta      — user token, conversations.history for ONE message
//                      (reply_count + reactions — search results carry
//                      reply_count but never reactions)
// Missing token / API error ⇒ {error}; callers render "unavailable: …".
// Bot users and the report channel's own posts are excluded from the
// unattended-mentions audit.

import type { FetchLike } from "./dialpadStats.js";

const API = "https://slack.com/api";

async function call(
  token: string,
  method: string,
  body: Record<string, unknown> | URLSearchParams,
  fetchImpl: FetchLike
): Promise<any> {
  const isForm = body instanceof URLSearchParams;
  const res = await fetchImpl(`${API}/${method}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": isForm ? "application/x-www-form-urlencoded" : "application/json; charset=utf-8",
    },
    body: isForm ? body.toString() : JSON.stringify(body),
    signal: AbortSignal.timeout(20_000),
  });
  const json: any = await res.json().catch(() => ({ ok: false, error: `non-JSON HTTP ${res.status}` }));
  if (!res.ok || !json.ok) throw new Error(`slack ${method}: ${json.error ?? `HTTP ${res.status}`}`);
  return json;
}

export interface PostResult {
  ts: string;
  channel: string;
  permalink?: string;
}

export async function postMessage(
  botToken: string | undefined,
  channel: string,
  text: string,
  blocks?: unknown[],
  fetchImpl: FetchLike = fetch
): Promise<PostResult | { error: string }> {
  if (!botToken) return { error: "no slack bot token (SLACK_BOT_TOKEN)" };
  if (!channel) return { error: "no slack channel configured" };
  try {
    const j = await call(botToken, "chat.postMessage", { channel, text, blocks, unfurl_links: false }, fetchImpl);
    let permalink: string | undefined;
    try {
      const p = await call(botToken, "chat.getPermalink", new URLSearchParams({ channel: j.channel, message_ts: j.ts }), fetchImpl);
      permalink = p.permalink;
    } catch {}
    return { ts: String(j.ts), channel: String(j.channel), permalink };
  } catch (err) {
    return { error: String((err as any)?.message ?? err).slice(0, 160) };
  }
}

export interface Mention {
  permalink: string;
  channel: string;
  channel_id: string;
  author: string;
  ts: string;
  ts_iso: string;
  subteams: string[];
  text: string;
  reply_count: number;
  reactions: number;
}

export interface MentionsAudit {
  count: number;
  scanned: number;
  window: { from: string; to: string };
  items: Mention[];
  note?: string;
}

const tsIso = (ts: string) => new Date(Math.floor(Number(ts) * 1000)).toISOString();

/** Unattended = zero replies AND zero reactions at audit time. */
export async function unattendedMentions(
  userToken: string | undefined,
  subteams: string[],
  fromMs: number,
  toMs: number,
  opts: { excludeChannel?: string; searchCap?: number; fetchImpl?: FetchLike } = {}
): Promise<MentionsAudit | { error: string }> {
  if (!userToken) return { error: "no slack user token (SLACK_USER_TOKEN)" };
  if (!subteams.length) return { error: "no slack subteams configured" };
  const fetchImpl = opts.fetchImpl ?? fetch;
  const cap = opts.searchCap ?? 60;
  const after = new Date(fromMs - 86_400_000).toISOString().slice(0, 10);
  const hits: any[] = [];
  try {
    for (let page = 1, seen = 0; page <= 4 && seen < cap; page++) {
      const j = await call(
        userToken,
        "search.messages",
        new URLSearchParams({ query: `member-support after:${after}`, sort: "timestamp", sort_dir: "desc", count: "50", page: String(page) }),
        fetchImpl
      );
      const matches = j.messages?.matches ?? [];
      hits.push(...matches);
      seen += matches.length;
      const pages = Number(j.messages?.paging?.pages ?? 1);
      if (page >= pages || !matches.length) break;
    }
  } catch (err) {
    return { error: String((err as any)?.message ?? err).slice(0, 160) };
  }
  const items: Mention[] = [];
  let scanned = 0;
  for (const m of hits) {
    const ms = Math.floor(Number(m.ts) * 1000);
    if (!(ms >= fromMs && ms < toMs)) continue;
    const text: string = m.text ?? "";
    const tagged = subteams.filter((s) => text.includes(`<!subteam^${s}>`));
    if (!tagged.length) continue;
    if (m.bot_id || m.subtype === "bot_message") continue;
    if (opts.excludeChannel && m.channel?.id === opts.excludeChannel) continue;
    scanned++;
    let replyCount = Number(m.reply_count ?? 0);
    let reactions = 0;
    try {
      const h = await call(
        userToken,
        "conversations.history",
        new URLSearchParams({ channel: m.channel.id, latest: m.ts, oldest: m.ts, inclusive: "true", limit: "1" }),
        fetchImpl
      );
      const msg = h.messages?.[0];
      if (msg) {
        replyCount = Number(msg.reply_count ?? replyCount);
        reactions = (msg.reactions ?? []).reduce((a: number, r: any) => a + Number(r.count ?? 0), 0);
      }
    } catch {
      // history denied (not a member of that channel): keep search's reply_count, reactions unknown → assume attended only if replies
    }
    if (replyCount === 0 && reactions === 0)
      items.push({
        permalink: m.permalink,
        channel: m.channel?.name ?? m.channel?.id ?? "?",
        channel_id: m.channel?.id ?? "",
        author: m.username ?? m.user ?? "?",
        ts: String(m.ts),
        ts_iso: tsIso(String(m.ts)),
        subteams: tagged,
        text: text.replace(/<!subteam\^[^>]+>/g, "@group").slice(0, 200),
        reply_count: replyCount,
        reactions,
      });
  }
  items.sort((a, b) => Number(b.ts) - Number(a.ts));
  return {
    count: items.length,
    scanned,
    window: { from: new Date(fromMs).toISOString(), to: new Date(toMs).toISOString() },
    items: items.slice(0, 40),
    note: hits.length >= cap ? `search capped at ${cap} hits` : undefined,
  };
}
