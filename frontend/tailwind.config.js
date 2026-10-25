/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,jsx,ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        'dss-bg': '#0a0a0a',
        'dss-card': '#141414',
        'dss-border': '#262626',
        'dss-accent': '#8b5cf6',
        'dss-accent-hover': '#7c3aed',
        'dss-text': '#f5f5f5',
        'dss-muted': '#a3a3a3',
        'dss-success': '#22c55e',
        'dss-error': '#ef4444',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
