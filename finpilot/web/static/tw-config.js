// Tailwind Play CDN configuration (loaded right after the CDN script).
// Text colours that must work in BOTH themes are semantic (CSS variables in /static/css/theme.css).
function fpVar(name) { return "rgb(var(--fp-" + name + ") / <alpha-value>)"; }
tailwind.config = {
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        brand: { 50: "#eef6ff", 100: "#d9eaff", 200: "#b8d4ff", 400: "#7aa5ff", 500: "#2f6fed", 600: "#2459c9", 700: "#1d47a1", 900: "#132c63" },
        muted: fpVar("muted"),
        soft: fpVar("soft"),
        accent: fpVar("accent"),
        pos: fpVar("pos"),
        neg: fpVar("neg"),
        warn: fpVar("warn"),
      },
      fontFamily: { sans: ["Inter Variable", "Inter", "ui-sans-serif", "system-ui", "sans-serif"] },
    },
  },
};
