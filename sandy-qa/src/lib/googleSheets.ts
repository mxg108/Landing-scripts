// Google Sheets v4 from a Worker — service-account JWT (RS256 via WebCrypto)
// → OAuth access token → REST. No gspread, no googleapis package.
// ShiftReport.md §10.3. Every call takes an injectable fetch so the test
// harness can assert the exact requests (tests/eod_report.test.mjs).
//
// Contract with qa-automation/AI-Scoring/scripts/ms_eod_report.py (the
// Python reference): identical tab headers, values written RAW (numbers stay
// numbers), rows for a date are REPLACED (col A), other rows/tabs untouched.

import type { FetchLike } from "./dialpadStats.js";

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const SHEETS = "https://sheets.googleapis.com/v4/spreadsheets";
const SCOPE = "https://www.googleapis.com/auth/spreadsheets";

export type CellValue = string | number | null;

export interface SheetsClient {
  spreadsheetId: string;
  token: string;
  fetchImpl: FetchLike;
}

// ── JWT / token ────────────────────────────────────────────────────────────

function b64url(bytes: ArrayBuffer | Uint8Array | string): string {
  const bin =
    typeof bytes === "string"
      ? bytes
      : String.fromCharCode(...new Uint8Array(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToDer(pem: string): ArrayBuffer {
  const body = pem.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, "");
  const bin = atob(body);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

/** Sign a service-account JWT and exchange it for a bearer token. */
export async function getAccessToken(
  serviceAccountJson: string,
  fetchImpl: FetchLike = fetch,
  nowMs: number = Date.now()
): Promise<string> {
  let sa: any;
  try {
    sa = JSON.parse(serviceAccountJson);
  } catch {
    throw new Error("GSHEETS_SA_JSON is not valid JSON");
  }
  if (!sa?.client_email || !sa?.private_key) throw new Error("GSHEETS_SA_JSON lacks client_email/private_key");
  const iat = Math.floor(nowMs / 1000);
  const header = b64url(new TextEncoder().encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const claims = b64url(
    new TextEncoder().encode(
      JSON.stringify({ iss: sa.client_email, scope: SCOPE, aud: TOKEN_URL, iat, exp: iat + 3600 })
    )
  );
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToDer(sa.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(`${header}.${claims}`)
  );
  const assertion = `${header}.${claims}.${b64url(sig)}`;
  const res = await fetchImpl(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion,
    }).toString(),
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`google token HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const json: any = await res.json();
  if (!json?.access_token) throw new Error("google token: no access_token");
  return String(json.access_token);
}

export async function openSpreadsheet(
  serviceAccountJson: string,
  spreadsheetId: string,
  fetchImpl: FetchLike = fetch,
  nowMs?: number
): Promise<SheetsClient> {
  const token = await getAccessToken(serviceAccountJson, fetchImpl, nowMs);
  return { spreadsheetId, token, fetchImpl };
}

// ── REST helpers ───────────────────────────────────────────────────────────

async function api(client: SheetsClient, path: string, init: RequestInit = {}): Promise<any> {
  const res = await client.fetchImpl(`${SHEETS}/${client.spreadsheetId}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${client.token}`,
      "Content-Type": "application/json",
      ...(init.headers as Record<string, string> | undefined),
    },
    signal: AbortSignal.timeout(60_000),
  });
  if (!res.ok) throw new Error(`sheets ${init.method ?? "GET"} ${path.split("?")[0]} HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

const quoteTab = (title: string) => `'${title.replace(/'/g, "''")}'`;

export interface TabMeta {
  sheetId: number;
  title: string;
  rowCount: number;
  columnCount: number;
}

export async function listTabs(client: SheetsClient): Promise<TabMeta[]> {
  const meta = await api(client, "?fields=sheets.properties(sheetId,title,gridProperties)");
  return (meta.sheets ?? []).map((s: any) => ({
    sheetId: s.properties.sheetId,
    title: s.properties.title,
    rowCount: s.properties.gridProperties?.rowCount ?? 0,
    columnCount: s.properties.gridProperties?.columnCount ?? 0,
  }));
}

export async function getValues(client: SheetsClient, title: string): Promise<CellValue[][]> {
  const json = await api(
    client,
    `/values/${encodeURIComponent(quoteTab(title))}?valueRenderOption=UNFORMATTED_VALUE`
  );
  return (json.values ?? []) as CellValue[][];
}

async function addTab(client: SheetsClient, title: string, rows: number, cols: number): Promise<TabMeta> {
  const json = await api(client, ":batchUpdate", {
    method: "POST",
    body: JSON.stringify({
      requests: [{ addSheet: { properties: { title, gridProperties: { rowCount: rows, columnCount: cols } } } }],
    }),
  });
  const p = json.replies?.[0]?.addSheet?.properties;
  return {
    sheetId: p.sheetId,
    title: p.title,
    rowCount: p.gridProperties?.rowCount ?? rows,
    columnCount: p.gridProperties?.columnCount ?? cols,
  };
}

/**
 * Replace the rows whose first cell (date) is in `dates`, append `newRows`,
 * rewrite the tab sorted with a frozen header. Header mismatch is an error
 * (never silently reshape someone's tab). Returns the data-row count.
 */
export async function upsertTab(
  client: SheetsClient,
  title: string,
  header: string[],
  newRows: CellValue[][],
  dates: Set<string>,
  sortKey: (row: CellValue[]) => string,
  tabs?: TabMeta[]
): Promise<number> {
  const all = tabs ?? (await listTabs(client));
  let tab = all.find((t) => t.title === title);
  let existing: CellValue[][] = [];
  if (!tab) {
    tab = await addTab(client, title, Math.max(newRows.length + 20, 100), header.length);
  } else {
    existing = await getValues(client, title);
  }
  if (existing.length) {
    const have = existing[0].map((v) => String(v ?? ""));
    if (have.join("") !== header.join(""))
      throw new Error(`tab ${title} has a different header than this job writes — archive/rename it first`);
  }
  const kept = existing.slice(1).filter((r) => r.length && !dates.has(String(r[0] ?? "")));
  const norm = (r: CellValue[]) => header.map((_, i) => (r[i] === undefined || r[i] === null ? "" : r[i]));
  const rows = [...kept, ...newRows].map(norm).sort((a, b) => (sortKey(a) < sortKey(b) ? -1 : sortKey(a) > sortKey(b) ? 1 : 0));
  const values = [header, ...rows];

  await api(client, `/values/${encodeURIComponent(quoteTab(title))}:clear`, { method: "POST", body: "{}" });
  await api(client, ":batchUpdate", {
    method: "POST",
    body: JSON.stringify({
      requests: [
        {
          updateSheetProperties: {
            properties: {
              sheetId: tab.sheetId,
              gridProperties: {
                rowCount: Math.max(values.length + 20, 100),
                columnCount: header.length,
                frozenRowCount: 1,
              },
            },
            fields: "gridProperties(rowCount,columnCount,frozenRowCount)",
          },
        },
      ],
    }),
  });
  await api(client, `/values/${encodeURIComponent(quoteTab(title) + "!A1")}?valueInputOption=RAW`, {
    method: "PUT",
    body: JSON.stringify({ range: `${quoteTab(title)}!A1`, majorDimension: "ROWS", values }),
  });
  return rows.length;
}
