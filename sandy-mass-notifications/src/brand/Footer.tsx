// App footer — slim navy strip for an internal tool. No marketing links.

import { Logomark } from "./Logo.js";

export function Footer() {
  return (
    <footer style={{ background: "var(--landing-blue)", color: "var(--landing-white)" }}>
      <div className="ds-container">
        <div
          className="flex items-center gap-3 flex-wrap"
          style={{ paddingTop: "var(--space-4)", paddingBottom: "var(--space-4)" }}
        >
          <Logomark height={22} color="var(--landing-white)" />
          <span className="body-xs" style={{ opacity: 0.75 }}>
            Mass Notifications · Member Support internal tool
          </span>
          <span className="flex-1" />
          <span className="body-xs" style={{ opacity: 0.55 }}>
            Sends as member.support@hellolanding.com · Sandy platform
          </span>
        </div>
      </div>
    </footer>
  );
}
