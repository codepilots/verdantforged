# VerdantForged — Marketing Site

Apple-style marketing site for the [VerdantForged TEE broker](https://verdant.codepilots.co.uk). This directory contains:

- The marketing site itself (Astro + Tailwind, single long landing page)
- Four pillar deep-dive pages (`/attestation`, `/security`, `/sandboxing`, `/payment`)
- The `/agents` page — for AI agents and the humans running them (setup, test broker access, submit jobs)
- The API reference (`/docs`), quickstart (`/quickstart`), pricing (`/pricing`), payment flow (`/payment-flow`), terms (`/terms`)
- `AGENT.md` — runnable instructions for AI agents (agent-first design)
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

## Live test broker

The broker is live at `https://verdant.codepilots.co.uk`. Demo mode is on by default
(no real card is charged). See the [agents page](https://verdantforged.pages.dev/agents)
for how to set up an agent and submit jobs.

## Deployment

Configured for Cloudflare Pages at **verdantforged.pages.dev** with a GitHub
Pages fallback. Push to `codepilots/verdantforged` on GitHub and the workflow
at `.github/workflows/pages.yml` rebuilds the site. PR previews are served
via the same workflow.

## Site structure

```
/                       ← landing page (hero → 4 pillars → how it works → security deep dive → demo → try)
/attestation/           ← pillar 01 deep dive
/security/              ← pillar 02 deep dive
/sandboxing/            ← pillar 03 deep dive
/payment/               ← pillar 04 deep dive
/agents/                ← for AI agents: setup, test broker access, submit jobs
/docs                   ← REST API reference (10 endpoints)
/quickstart             ← 5-minute walkthrough (working Python script)
/pricing                ← cost model (session lease + per-1K-token + Stripe fees)
/payment-flow           ← the 4 lifecycle paths (happy / failure / short funds / abandoned topup)
/terms                  ← hackathon terms stub
/AGENT.md               ← agent-runnable instructions
/openapi.json           ← OpenAPI 3.1 spec for the live broker
/.well-known/agent.json ← A2A discovery manifest
```

## For humans

Read `PROPOSAL.md` first. The visual identity, build order, and open decisions
are all there. The `verdantforged-marketing-site` skill in `~/.hermes/skills/`
carries the same context.

## For agents

If you are an AI agent reading this README, your entry point is
[`AGENT.md`](./AGENT.md).
Follow the instructions there to use the test broker or set up your own.

## Project layout

```
tee-broker-site/
├── AGENT.md                  ← agent instructions (discoverable)
├── README.md                 ← this file
├── PROPOSAL.md               ← design rationale + palette + build plan
├── CHANGELOG.md              ← change history
├── astro.config.mjs          ← Astro configuration
├── tailwind.config.ts        ← Tailwind config with locked palette tokens
├── tsconfig.json
├── package.json
├── public/
│   ├── .well-known/agent.json
│   ├── openapi.json
│   └── favicon.svg
├── src/
│   ├── layouts/BaseLayout.astro
│   ├── layouts/PillarLayout.astro   ← shared shell for the 4 pillar deep dives
│   ├── pages/                       ← 12 routes total
│   ├── components/                  ← Hero, Pillars, Footer, etc.
│   └── styles/global.css
└── content/
    └── copy.md               ← all marketing copy in one place
```

## Do not

- Do not edit anything in `tee-broker-pattern/` or `tee-broker-docs-archive-2026-06-29/` from this directory
- Do not use stock photography — site uses real screenshots and abstract motion
- Do not use clichéd AI imagery (neural nets, glowing brains, robot heads)
- Do not ship without `npm run build` passing cleanly first

## License

MIT — same as the underlying tee-broker-pattern.

## Acknowledgments

This project stands on the work of many people and projects. In particular:

- **[NVIDIA NemoClaw](https://developer.nvidia.com/)** — the AMD SEV-SNP hardware isolation that the broker runs inside
- **[Stripe](https://stripe.com/)** — the PaymentIntent verify-then-capture lifecycle
- **[Nous Research](https://nousresearch.com/)** — Hermes inference and the broader Hermes agent architecture
- All the open source libraries: Astro, Tailwind, Inter, JetBrains Mono
