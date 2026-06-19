import type { Config } from 'tailwindcss';

// VerdantForged palette — copper + verdigris + temperate rainforest
// LOCKED 2026-06-19 (see PROPOSAL.md)
const config: Config = {
  content: ['./src/**/*.{astro,html,js,jsx,md,ts,tsx,vue,svelte}'],
  theme: {
    extend: {
      colors: {
        bg: '#faf8f4',         // warm off-white
        ink: '#1a1a17',        // warm near-black
        'ink-soft': '#4a4a45', // secondary text
        copper: '#b87333',     // primary accent — forged, warm
        'copper-deep': '#7d4a23', // hover, aged copper
        verdigris: '#5fa39a',  // copper-oxide green — verified states
        'verdigris-deep': '#2d6a4f', // dark accent — VerdantFamiliar green
        moss: '#8a9a5b',       // tertiary — fern-leaf, dividers
        bark: '#3b2e23',       // footer / code — wet bark
      },
      fontFamily: {
        sans: ['Inter', 'Inter Tight', 'SF Pro Display', '-apple-system', 'BlinkMacSystemFont', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'SF Mono', 'ui-monospace', 'monospace'],
        display: ['Inter Tight', 'Inter', 'SF Pro Display', 'system-ui', 'sans-serif'],
      },
      fontSize: {
        // Apple-style scale
        'display-xl': ['96px', { lineHeight: '1.05', letterSpacing: '-0.04em', fontWeight: '600' }],
        'display-lg': ['72px', { lineHeight: '1.05', letterSpacing: '-0.035em', fontWeight: '600' }],
        'display-md': ['56px', { lineHeight: '1.1', letterSpacing: '-0.03em', fontWeight: '600' }],
        'display-sm': ['40px', { lineHeight: '1.15', letterSpacing: '-0.025em', fontWeight: '600' }],
        'body-lg': ['21px', { lineHeight: '1.5', letterSpacing: '-0.011em' }],
        'body': ['17px', { lineHeight: '1.55', letterSpacing: '-0.011em' }],
        'body-sm': ['14px', { lineHeight: '1.5', letterSpacing: '-0.005em' }],
        'caption': ['12px', { lineHeight: '1.4', letterSpacing: '0.02em' }],
      },
      spacing: {
        '18': '4.5rem',
        '22': '5.5rem',
        '30': '7.5rem',
        '38': '9.5rem',
        'section': '100vh',
      },
      maxWidth: {
        'reading': '38rem',
        'prose-vf': '52rem',
        'wide': '88rem',
      },
      transitionTimingFunction: {
        'apple': 'cubic-bezier(0.25, 0.1, 0.25, 1)',
        'apple-snap': 'cubic-bezier(0.4, 0, 0.2, 1)',
      },
      transitionDuration: {
        '250': '250ms',
        '400': '400ms',
      },
      keyframes: {
        'fade-up': {
          '0%': { opacity: '0', transform: 'translateY(20px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'pulse-verdigris': {
          '0%, 100%': { opacity: '0.6' },
          '50%': { opacity: '1' },
        },
      },
      animation: {
        'fade-up': 'fade-up 600ms cubic-bezier(0.25, 0.1, 0.25, 1) both',
        'fade-in': 'fade-in 800ms cubic-bezier(0.25, 0.1, 0.25, 1) both',
        'pulse-verdigris': 'pulse-verdigris 3s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};

export default config;
