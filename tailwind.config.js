module.exports = {
  content: [
    "./templates/**/*.html",
    "./core/templates/**/*.html",
    "./core/**/*.html",
    "./**/templates/**/*.html"
  ],
  safelist: [
    "hidden",
    "flex",
    "grid",
    "block",
    "inline-flex",
    "lg:hidden",
    "lg:flex",
    "min-h-screen",
    "h-full",
    "dark",
    "glass",
    "gradient-accent",
    "text-accent",
    "bg-accent",
    "shadow-glow",
    "animate-float",
    "animate-shine"
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "Segoe UI", "Roboto", "Helvetica Neue", "Arial"]
      },
      colors: {
        brand: {
          50: "#f5fbff",
          100: "#e6f6ff",
          400: "#10b981",
          500: "#0ea5a0",
          700: "#047857"
        }
      },
      boxShadow: {
        glow: "0 10px 30px -5px rgba(16,185,129,0.35), 0 8px 16px -8px rgba(59,130,246,0.25)"
      },
      keyframes: {
        float: {
          "0%,100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-8px)" }
        },
        shine: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" }
        }
      },
      animation: {
        float: "float 8s ease-in-out infinite",
        shine: "shine 2.5s linear infinite"
      }
    }
  },
  plugins: []
}