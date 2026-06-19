// @ts-check
import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

// https://astro.build/config
export default defineConfig({
  // Primary domain is the future Cloudflare Pages URL.
  // Until Cloudflare credentials are wired, the live site lives at:
  //   https://codepilots.github.io/verdantforged/
  // Astro's <link rel="canonical"> and OG tags use `site` to compute
  // absolute URLs; `base` adds the project path prefix for assets so
  // they resolve correctly on GitHub Pages (/_astro/foo.css) AND
  // on Cloudflare Pages (/_astro/foo.css) without code changes.
  site: 'https://verdantforged.pages.dev',
  base: '/verdantforged',
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
