/// <reference types="vite/client" />

// Raw SVG source (used to inline icons/logos so `currentColor` theming works).
declare module "*.svg?raw" {
  const src: string;
  export default src;
}
