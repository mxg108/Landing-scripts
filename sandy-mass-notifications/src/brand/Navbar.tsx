// App header — dark navy bar echoing the original GAS WebApp chrome:
// white Landing wordmark, app title, real nav links (no placeholders),
// and the signed-in operator on the right.

import { Wordmark } from "./Logo.js";

interface NavbarProps {
  userEmail?: string;
  active?: "campaigns" | "edit";
}

const LINKS: { key: "campaigns" | "edit"; href: string; label: string }[] = [
  { key: "campaigns", href: "/", label: "Campaigns" },
  { key: "edit", href: "/edit", label: "Cards & disclaimers" },
];

export function Navbar({ userEmail, active = "campaigns" }: NavbarProps) {
  return (
    <header
      className="sticky top-0 z-50"
      style={{ background: "var(--landing-blue)", borderBottom: "3px solid var(--landing-bright-blue)" }}
    >
      <div className="ds-container">
        <div className="flex items-center gap-4" style={{ height: 60 }}>
          <a href="/" className="flex items-center gap-3" style={{ textDecoration: "none" }}>
            <Wordmark height={18} color="var(--landing-white)" />
            <span
              className="label-sm"
              style={{
                color: "var(--landing-white)", opacity: 0.85,
                borderLeft: "1px solid rgba(255,255,255,0.3)", paddingLeft: 12,
                letterSpacing: "0.04em",
              }}
            >
              Mass Notifications
            </span>
          </a>
          <nav className="flex items-center gap-5 ml-4">
            {LINKS.map((l) => (
              <a
                key={l.key}
                href={l.href}
                className="label-sm no-underline hover:no-underline"
                style={{
                  color: "var(--landing-white)",
                  opacity: l.key === active ? 1 : 0.65,
                  borderBottom: l.key === active ? "2px solid var(--landing-bright-blue)" : "2px solid transparent",
                  paddingBottom: 2,
                }}
              >
                {l.label}
              </a>
            ))}
          </nav>
          <div className="flex-1" />
          {userEmail && (
            <span className="body-xs hidden sm:inline" style={{ color: "var(--landing-white)", opacity: 0.6 }}>
              {userEmail}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
