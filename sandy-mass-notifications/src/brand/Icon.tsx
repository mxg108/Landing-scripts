// Inline SVG icons from the Landing design system (src/design-system/assets/icons).
//
// Icons are imported as raw SVG source and inlined into the DOM so they inherit
// `currentColor` — set the color via `color` / Tailwind `text-*` on the icon or
// any ancestor. The source SVGs hardcode the brand navy (#15192D); we rewrite it
// to currentColor at module load so a single icon works on light and dark surfaces.

const rawIcons = import.meta.glob("../design-system/assets/icons/*.svg", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Build a name -> themeable SVG string map. "bed-bold.svg" -> "bed-bold".
const icons: Record<string, string> = {};
for (const [path, svg] of Object.entries(rawIcons)) {
  const name = path.split("/").pop()!.replace(/\.svg$/, "");
  icons[name] = svg.replace(/#15192D/gi, "currentColor");
}

export type IconName = keyof typeof icons;

// All available icon names (useful for pickers / validation).
export const iconNames = Object.keys(icons).sort();

type IconSize = "sm" | "md" | "lg";
const sizeClass: Record<IconSize, string> = {
  sm: "icon-sm",
  md: "",
  lg: "icon-lg",
};

interface IconProps {
  name: IconName;
  size?: IconSize;
  className?: string;
  title?: string;
}

export function Icon({ name, size = "md", className = "", title }: IconProps) {
  const svg = icons[name];
  if (!svg) return null;
  return (
    <span
      className={`icon ${sizeClass[size]} ${className}`.trim()}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
