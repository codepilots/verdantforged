# VerdantForged — Marketing Site

Apple-style single-page marketing site for the [VerdantForged TEE Broker Agent Marketplace](../tee-broker-docs/SPEC.md). This directory contains:

- The marketing site itself (Astro + Tailwind, single long page)
- `.Agent.md` and `AGENT.md` — runnable instructions for AI agents (agent-first design)
- `.well-known/agent.json` — A2A discovery manifest
- `PROPOSAL.md` — the design rationale, palette decisions, and build plan
- `content/copy.md` — all marketing copy in one file for easy editing

## Quick start

```bash
npm install
npm run dev          # http://localhost:4321
npm run build        # static export to ./dist
npm run preview      # preview the build
```

## Deployment

Configured for Cloudflare Pages at **verdantforged.pages.dev**.

### Automatic deploy via GitHub Actions

1. Push to `codepilots/verdantforged` on GitHub (already configured as remote `github`)
2. Add two secrets in repo Settings → Secrets and variables → Actions:
   - `CLOUDFLARE_API_TOKEN` — Cloudflare API token with `Cloudflare Pages: Edit` permission
     (create at https://dash.cloudflare.com/profile/api-tokens → Create Token → Edit Cloudflare Pages)
   - `CLOUDFLARE_ACCOUNT_ID` — found on the Cloudflare dashboard right sidebar
3. Create the Cloudflare Pages project (one-time):
   ```bash
   npx wrangler pages project create verdantforged --production-branch main
   ```
4. Every push to `main` triggers the GitHub Actions workflow at `.github/workflows/deploy.yml`,
   which builds the site and deploys to `https://verdantforged.pages.dev`.
   PRs get preview URLs at `https://<branch>.verdantforged.pages.dev`.

### Manual deploy

```bash
npm run build                                  # build to ./dist
npx wrangler login                             # one-time OAuth
npx wrangler pages deploy dist --project-name verdantforged
```

### Files controlling Cloudflare behavior

- `wrangler.toml` — Pages project config, build output dir
- `public/_headers` — security headers (X-Frame-Options, CSP), cache policy per asset type
- `public/_redirects` — friendly URLs for agent discovery (`/agent.json` → `/.well-known/agent.json`)

### Custom domain

Once `verdantforged.com` is registered (10 TLDs are still available as of 2026-06-19),
add it in Cloudflare Pages → Custom domains. Both `verdantforged.com` and
`verdantforged.pages.dev` will serve the same content; set a 301 redirect from
`pages.dev` to the custom domain in `_redirects`.

## For humans

Read `PROPOSAL.md` first. The visual identity, build order, and open decisions are all there. The skill `verdantforged-marketing-site` in `~/.hermes/skills/` carries the same context.

## For agents

If you are an AI agent reading this README, your entry point is [`AGENT.md`](./AGENT.md) — the same file is also discoverable as `.Agent.md`. Follow the instructions there to install the VerdantForged skill.

## Project layout

```
tee-broker-site/
├── .Agent.md                 ← agent instructions (hidden, alt name)
├── AGENT.md                  ← agent instructions (mirrored, more discoverable)
├── README.md                 ← this file
├── PROPOSAL.md               ← design rationale + palette + build plan
├── astro.config.mjs          ← Astro configuration
├── tailwind.config.ts        ← Tailwind config with locked palette tokens
├── tsconfig.json
├── package.json
├── public/
│   ├── .well-known/agent.json
│   └── favicon.svg
├── src/
│   ├── layouts/BaseLayout.astro
│   ├── pages/index.astro
│   ├── pages/api/agent-init.ts
│   ├── components/
│   └── styles/global.css
└── content/
    └── copy.md               ← all marketing copy in one place
```

## Do not

- Do not edit anything in `tee-broker-pattern/` or `tee-broker-docs/` from this directory
- Do not use stock photography — site uses real screenshots and abstract motion
- Do not use clichéd AI imagery (neural nets, glowing brains, robot heads)
- Do not ship without `npm run build` passing cleanly first

## License

MIT — same as the underlying tee-broker-pattern.
