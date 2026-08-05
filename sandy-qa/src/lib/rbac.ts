// Per-team/per-role RBAC (pinned design 2026-08-03) — roles resolve
// AUTOMATICALLY from the Cf-Access-Jwt-Assertion email Google SSO already
// injects at the Sandy edge. There is no login, no session, no auth UX:
// users discover restrictions only by hitting one, and the denial page
// offers a self-service access request (qa_access_requests; /admin is the
// inbox until the Slack notification integration is designed properly).
//
// Roles:
//   admin   — everything + /admin (role grants, request triage)
//   qa      — privileged evaluator: lookup, scoring console, all actions
//   manager — RESERVED (per-team semantics land when manager capabilities
//             are defined; today it maps to viewer)
//   viewer  — any SSO-authenticated employee: dashboards, drill-downs
//
// LOOKUP_ALLOW (the interim secret) stays as a grandfather bridge: members
// without a qa_roles row act as 'qa' until the secret is deleted. A roles
// row always wins over the bridge.

export type Role = "admin" | "qa" | "manager" | "viewer";

export interface Access {
  email: string;
  role: Role;
  /** admin|qa — may use lookup, trigger scoring, and run scorecard actions */
  privileged: boolean;
}

// SSO identity: signature validation happens at the Access edge before the
// request reaches the worker (Engineering confirmation of the header
// guarantee is a standing sit-down item).
export function accessEmail(request: Request): string {
  const jwt =
    request.headers.get("Cf-Access-Jwt-Assertion") ??
    request.headers.get("cf-access-jwt-assertion") ??
    "";
  const parts = jwt.split(".");
  if (parts.length < 2) return "";
  try {
    const payload = JSON.parse(
      atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"))
    );
    return (payload.email ?? "").toLowerCase();
  } catch {
    return "";
  }
}

export async function resolveAccess(
  request: Request,
  db: D1Database,
  lookupAllow?: string
): Promise<Access> {
  const email = accessEmail(request);
  if (!email) return { email: "", role: "viewer", privileged: false };
  const row = await db
    .prepare("SELECT role FROM qa_roles WHERE email = ?")
    .bind(email)
    .first<{ role: Role }>();
  if (row) {
    const privileged = row.role === "admin" || row.role === "qa";
    return { email, role: row.role, privileged };
  }
  if (
    lookupAllow &&
    lookupAllow
      .split(",")
      .map((e) => e.trim().toLowerCase())
      .filter(Boolean)
      .includes(email)
  ) {
    return { email, role: "qa", privileged: true };
  }
  return { email, role: "viewer", privileged: false };
}
