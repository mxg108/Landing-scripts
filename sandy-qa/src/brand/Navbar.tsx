// Landing top navigation — sticky, translucent white with a soft blur, wordmark
// on the left, nav links, and a primary CTA. Mirrors the design system's
// marketing-website navbar. Purely presentational (links are placeholders).

import { Wordmark } from "./Logo.js";

const NAV_ITEMS = ["Stays", "Cities", "For business", "About"];

interface NavbarProps {
  active?: string;
}

export function Navbar({ active = "Stays" }: NavbarProps) {
  return (
    <header
      className="sticky top-0 z-50"
      style={{
        background: "rgba(255,255,255,0.92)",
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
        borderBottom: "1px solid var(--border-secondary)",
      }}
    >
      <div className="ds-container">
        <div className="flex items-center gap-8" style={{ height: 80 }}>
          <Wordmark height={22} />
          <nav className="hidden md:flex items-center gap-7 ml-2">
            {NAV_ITEMS.map((item) => (
              <a
                key={item}
                href="#"
                className="font-sans text-sm no-underline hover:no-underline"
                style={{
                  fontWeight: "var(--fw-medium)" as unknown as number,
                  color: item === active ? "var(--landing-blue)" : "var(--text-secondary)",
                }}
              >
                {item}
              </a>
            ))}
          </nav>
          <div className="flex-1" />
          <a
            href="#"
            className="hidden sm:inline text-sm no-underline"
            style={{ fontWeight: "var(--fw-medium)" as unknown as number, color: "var(--landing-blue)" }}
          >
            Sign in
          </a>
          <button type="button" className="btn btn-primary btn-sm">
            Book a stay
          </button>
        </div>
      </div>
    </header>
  );
}
