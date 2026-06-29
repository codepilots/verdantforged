# Changelog

All notable changes to the VerdantForged site.

## [Unreleased]

### New content (2026-06-29)
- **Four pillar deep-dive pages** — beginner-friendly explainers for Attestation (`/attestation`), Security (`/security`), Sandboxing (`/sandboxing`), Payment (`/payment`). Each page has: the problem (with a non-jargon analogy), how the broker does it (3 steps with concrete primitives), the attack/defense matrix, and a "look at the code" section pointing at the audit + live broker.
- **`/agents` page** — for AI agents and the humans running them. End-to-end: check the broker is alive, register a skill, submit a job, poll for the result, decrypt, run your own broker. Copy-pasteable curl, exercised against `verdant.codepilots.co.uk`.
- **PillarLayout.astro** — shared shell for the 4 deep-dive pages, so each page just supplies content.
- **Pillars.astro** updated: each pillar card now links to its deep-dive page. New "For agents" callout at the bottom pointing at `/agents`.
- **Hero.astro** updated: CTAs now go to "See the four pillars" / "For agents" / "Read the security audit". Subhead rewritten to mention the live test broker.
- **Footer.astro** updated: added a "Four pillars" column linking to the deep-dive pages and a "For agents" entry. Resources strip with protocol spec, audit, source code, AGENT.md.
- **SiteNav.astro** updated: added Attestation, Security, Sandboxing, Payment, For agents to the global nav.

### Polish (2026-06-29)
- **`/pricing` rewritten** — removed all "Skill Provider" / marketplace framing. Two-line cost model unchanged (lease + LLM tokens + Stripe fees), now with a demo-mode callout pointing at the live test broker and cross-links to `/payment-flow` and `/terms`.
- **`/docs` updated** — added SiteNav header and an "/agents" cross-link in the intro.
- **`/quickstart` updated** — added SiteNav header and an "/agents" cross-link.
- **`/payment-flow` updated** — replaced the (now-stale) "API contract" + "Security review" cross-link cards with three new cards pointing at `/payment` (deep dive), `/docs` (API), and `/agents` (setup).
- **`/terms` updated** — added a small cross-link in the hero to the engineering view (`/payment`) and the operator setup (`/agents`).
- **`AGENT.md` and `.Agent.md` rewritten** — drop all marketplace framing. Four-pillar pitch, live test broker URLs, demo + production submit loops, full security guarantees table with the latest primitives, updated for the X25519 + ChaCha20-Poly1305 production path.
- **`README.md` updated** — site structure now lists all 12 pages. Live test broker called out at the top.

### Earlier (2026-06-29 morning commit e02f3c4)
- **Pivot: reframe site as broker-as-product, lead with four pillars** — removed Provider / Requester framing, the 3-agent TrustLoop, the in-browser Pyodide splash, the marketplace SkillShowcase, the session dashboard tile. Added the four-pillar centerpiece. Hero rewritten. TryInAgent, Footer, BaseLayout defaults all updated to match. CHANGELOG entry captured.

### Earlier
- Added Stripe chargeback and dispute clause to Terms of Service.
- Added cross-link from Payment Flow documentation to dispute terms.
