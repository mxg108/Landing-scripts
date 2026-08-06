// Landing logos (from src/design-system/assets).
//
// Both are inlined as raw SVG so their paths inherit `currentColor` (see the
// `.ds-logo` rules in styles.css). Set the color on the wrapper to place the
// logo on light (navy, the default) or dark (white) surfaces.

import wordmarkSvg from "../design-system/assets/logo.svg?raw";
import logomarkSvg from "../design-system/assets/landing-logomark-landing-white.svg?raw";

interface LogoProps {
  // Pixel height; width scales from the SVG aspect ratio.
  height?: number;
  className?: string;
  // Override color (defaults to navy via .ds-logo). Pass "var(--landing-white)"
  // on dark backgrounds.
  color?: string;
}

// Serif wordmark. Minimum legible width is ~120px.
export function Wordmark({ height = 24, className = "", color }: LogoProps) {
  return (
    <span
      className={`ds-logo ${className}`.trim()}
      style={{ height, color }}
      role="img"
      aria-label="Landing"
      dangerouslySetInnerHTML={{ __html: sized(wordmarkSvg, height) }}
    />
  );
}

// Abstract "L" logomark. Square; minimum 24×24.
export function Logomark({ height = 32, className = "", color }: LogoProps) {
  return (
    <span
      className={`ds-logo ${className}`.trim()}
      style={{ height, color }}
      role="img"
      aria-label="Landing"
      dangerouslySetInnerHTML={{ __html: sized(logomarkSvg, height) }}
    />
  );
}

// Force the intrinsic height so the SVG lays out predictably; width stays auto
// (driven by viewBox) via the `.ds-logo svg` rule.
function sized(svg: string, height: number): string {
  return svg.replace(/<svg /, `<svg height="${height}" `);
}
