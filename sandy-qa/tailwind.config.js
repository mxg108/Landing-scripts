/** @type {import('tailwindcss').Config} */
// Tailwind is mapped onto the Landing design system tokens so utility classes
// (bg-*, text-*, rounded-*, font-*) stay on-brand. The raw CSS variables live in
// src/design-system/colors_and_type.css; this exposes the common ones to Tailwind.
export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        landing: {
          blue: "var(--landing-blue)",
          "bright-blue": "var(--landing-bright-blue)",
          white: "var(--landing-white)",
          "tonal-blue": "var(--landing-tonal-blue)",
          "baby-blue": "var(--landing-baby-blue)",
          "special-blue": "var(--landing-special-blue)",
          "utility-blue": "var(--landing-utility-blue)",
        },
        bg: {
          primary: "var(--bg-primary)",
          secondary: "var(--bg-secondary)",
          emphasis: "var(--bg-emphasis)",
          dark: "var(--bg-dark)",
        },
        text: {
          primary: "var(--text-primary)",
          secondary: "var(--text-secondary)",
          tertiary: "var(--text-tertiary)",
          link: "var(--text-link)",
        },
        border: {
          primary: "var(--border-primary)",
          secondary: "var(--border-secondary)",
        },
        status: {
          "success-text": "var(--status-success-text)",
          "success-bg": "var(--status-success-bg)",
          "error-text": "var(--status-error-text)",
          "error-bg": "var(--status-error-bg)",
          "info-text": "var(--status-info-text)",
          "info-bg": "var(--status-info-bg)",
        },
      },
      fontFamily: {
        sans: ["Saans", "Moderat", "sans-serif"],
        display: ["Moderat", "Saans", "sans-serif"],
        mono: ["Moderat Mono", "ui-monospace", "monospace"],
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
        xl: "var(--radius-xl)",
        pill: "var(--radius-pill)",
      },
      boxShadow: {
        card: "var(--shadow-card)",
        elevated: "var(--shadow-elevated)",
        up: "var(--shadow-up)",
      },
    },
  },
  plugins: [],
};
