// @ts-check
import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

// https://astro.build/config
export default defineConfig({
  site: 'https://verdantforged.pages.dev',
  integrations: [tailwind()],
  // Allow .well-known/ directory to be served (for A2A agent discovery)
  trailingSlash: 'ignore',
  vite: {
    ssr: {
      noExternal: ['gsap', 'lenis'],
    },
  },
  // Headers for the agent-first design
  server: {
    headers: {
      'X-Frame-Options': 'DENY',
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
    },
  },
});
