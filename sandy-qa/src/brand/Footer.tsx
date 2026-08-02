// Landing footer — dark navy surface with the white logomark, tagline, and link
// columns. Mirrors the design system's marketing footer pattern.

import { Logomark } from "./Logo.js";

const COLUMNS: { title: string; links: string[] }[] = [
  { title: "Company", links: ["About", "Careers", "Press", "Contact"] },
  { title: "Stays", links: ["Find a Landing", "Cities", "For business", "Refer a friend"] },
  { title: "Support", links: ["Help center", "Concierge", "Trust & safety", "Terms"] },
];

export function Footer() {
  return (
    <footer style={{ background: "var(--landing-blue)", color: "var(--landing-white)" }}>
      <div className="ds-container">
        <div
          className="grid gap-10 md:grid-cols-[1.5fr_1fr_1fr_1fr]"
          style={{ paddingTop: "var(--space-9)", paddingBottom: "var(--space-7)" }}
        >
          <div className="flex flex-col gap-4">
            <Logomark height={40} color="var(--landing-white)" />
            <p
              className="font-display"
              style={{ color: "var(--landing-white)", maxWidth: 260, lineHeight: "var(--lh-140)", margin: 0 }}
            >
              Where every stay feels like home.
            </p>
          </div>
          {COLUMNS.map((col) => (
            <div key={col.title} className="flex flex-col gap-3">
              <span
                className="label-xs"
                style={{ color: "var(--landing-medium-grey)", textTransform: "uppercase", letterSpacing: "0.04em" }}
              >
                {col.title}
              </span>
              {col.links.map((link) => (
                <a
                  key={link}
                  href="#"
                  className="text-sm no-underline hover:underline"
                  style={{ color: "var(--landing-white)" }}
                >
                  {link}
                </a>
              ))}
            </div>
          ))}
        </div>
        <div
          className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2"
          style={{
            borderTop: "1px solid var(--landing-tonal-blue)",
            paddingTop: "var(--space-5)",
            paddingBottom: "var(--space-7)",
          }}
        >
          <span className="body-sm" style={{ color: "var(--landing-medium-grey)" }}>
            © Landing. Wherever life takes you.
          </span>
          <div className="flex gap-5">
            <a href="#" className="body-sm no-underline hover:underline" style={{ color: "var(--landing-medium-grey)" }}>
              Privacy
            </a>
            <a href="#" className="body-sm no-underline hover:underline" style={{ color: "var(--landing-medium-grey)" }}>
              Terms
            </a>
          </div>
        </div>
      </div>
    </footer>
  );
}
