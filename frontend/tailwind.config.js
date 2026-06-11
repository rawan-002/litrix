/**
 * Tailwind config — Apple-inspired design tokens.
 *
 * Color palette: monochrome with one accent. Space is the design.
 * Typography: Inter as the system font (great Arabic + English coverage).
 */
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/**/*.{html,ts}"],
  theme: {
    extend: {
      fontFamily: {
        // Latin renders in Inter; Arabic has no Inter glyphs so the browser
        // falls through to Cairo (a modern Arabic face that pairs with Inter).
        sans: ['Inter', 'Cairo', '-apple-system', 'BlinkMacSystemFont',
               'Segoe UI', 'Roboto', 'sans-serif'],
      },
      colors: {
        ink: {
          50:  '#fafafa',
          100: '#f5f5f7',
          200: '#e8e8ed',
          300: '#d2d2d7',
          400: '#86868b',
          500: '#6e6e73',
          600: '#424245',
          700: '#1d1d1f',
          900: '#000000',
        },
        accent: {
          DEFAULT: '#0071e3',
          hover:   '#0077ed',
          light:   '#e8f1fc',
        },
      },
      boxShadow: {
        'card':   '0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06)',
        'hover':  '0 8px 24px rgba(0,0,0,0.08)',
      },
      borderRadius: {
        'apple': '14px',
      },
    },
  },
  plugins: [],
};
