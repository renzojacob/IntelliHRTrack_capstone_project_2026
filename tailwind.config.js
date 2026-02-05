module.exports = {
  content: [
    "./templates/**/*.html",
    "./**/templates/**/*.html",
  ],
  safelist: [
    'w-72',
    'lg:flex',
    'hidden',
    'min-h-screen',
    'flex',
    'grid',
    'block',
    'inline-flex'
  ],
  safelistPatterns: [
    /^(bg|text|from|to|border|ring|col|grid|gap|p|px|py|pl|pr|pt|pb|m|mx|my|w|h|min-h|max-w|rounded|shadow|translate|scale|hover|focus|lg|md):?/,
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
