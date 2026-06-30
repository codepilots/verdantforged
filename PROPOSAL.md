# Graftwood — Proposal for TEE Broker Marketing Site

**Status:** Concept proposal (2026-06-19), awaiting name-conflict resolution
**Project root:** `~/hermes/competition/tee-broker-site/`
**Related work (do NOT touch):** `tee-broker-pattern/` (Rust core, 5 crates, 46 tests), `tee-broker-docs-archive-2026-06-29/`

---

## Concept

Apple-style marketing site for the existing TEE Broker Agent Marketplace (NVIDIA × Stripe × Nous Research hackathon, deadline EOD June 30 2026). The site advertises the *existing* protocol — it does not redevelop it.

### Name: VerdantForged

- **Why:** "Verdant" (green, growing) + "Forged" (made strong, hammered into shape). Names the product as something *grown-and-tempered* — fits the VerdantFamiliar thread and the SEV-SNP "hardware-attested" reality (attestation is the forge that makes the protocol trustworthy). Verbs well: *to VerdantForge* = to attest and execute.
- **Status:** Uniqueness verified 2026-06-19. ALL CLEAR:
  - GitHub orgs: 0 hits (404 on `github.com/verdantforged`)
  - GitHub repos: 0 hits
  - npm: 404 (no package)
  - PyPI: not found via JSON API (HTTP 200 was a soft-404 page)
  - crates.io: "crate `verdantforged` does not exist"
  - Moltbook / Nostr: zero hits for the agent name
  - Trademark / Companies House: zero hits
  - One Pinterest inspiration board exists (Carlos-Angel Robles-Romero, 36 pins) — non-commercial hobby, not a collision
  - Amazon self-published fantasy novel uses "Verdant Vow: Forged in Magic" — different phrase, different category
- **Domains:** ALL 10 candidate TLDs are unregistered (no NS records, whois "No match"):
  - `verdantforged.com` ✅
  - `verdantforged.io` ✅
  - `verdantforged.ai` ✅
  - `verdantforged.market` ✅ (my recommendation — matches the product)
  - `verdantforged.marketplace` ✅
  - `verdantforged.shop` ✅
  - `verdantforged.exchange` ✅
  - `verdantforged.dev` ✅
  - `verdantforged.app` ✅
  - `verdant-forged.com` ✅ (hyphenated variant)

### Color palette — copper + verdigris + temperate rainforest (LOCKED 2026-06-19)

The "forged in a rainforest" feel. Off-white background, near-black text, copper as the warm human signal, verdigris as the cool "attested/aged" signal, with forest-floor textures in imagery. Real copper has a specific patina arc (bright → dark → green) and we use the whole arc.

| Token | Hex | Use |
|---|---|---|
| `--bg` | `#faf8f4` | Page background — warm off-white, paper-like |
| `--ink` | `#1a1a17` | Body text — warm near-black, not blue-black |
| `--ink-soft` | `#4a4a45` | Captions, secondary text |
| `--copper` | `#b87333` | Primary accent — buttons, links, highlights. The warm "forged" signal. |
| `--copper-deep` | `#7d4a23` | Hover state, strong borders. Aged copper. |
| `--verdigris` | `#5fa39a` | Secondary accent — tags, "verified" states. Copper-oxide green. |
| `--verdigris-deep` | `#2d6a4f` | Dark accent — hero type, bold claims. Deep forest. The VerdantFamiliar green. |
| `--moss` | `#8a9a5b` | Tertiary — subtle backgrounds, dividers, fern-leaf color. |
| `--bark` | `#3b2e23` | Footer, code blocks, dark sections. Wet bark. |

**Typography rules:**
- Headlines: Inter Tight or similar geometric, weight 600, color `--verdigris-deep` for hero, `--ink` for body
- Links: `--copper`, underline-on-hover transitions to `--copper-deep`
- Code: `--bark` bg, `--copper` for keywords

**Imagery rules:**
- Temperate rainforest: ferns, moss, lichen, dappled light, wet bark, fallen leaves
- Avoid: tropical jungle, desert, cacti, anything arid
- Source priorities: own photography if possible, then Unsplash (free commercial use) with explicit `temperate forest`, `fern`, `moss`, `rainforest` queries
- No AI-generated imagery for hero — looks uncanny on close inspection
- Diagrammatic imagery (the 3-agent flow, SEV-SNP chip render) can be SVG/CSS, animated

**Texture / finish feel:**
- Subtle paper-grain texture on `--bg` (CSS noise pattern, ~2% opacity)
- Buttons: subtle inner shadow at top, slight gradient `--copper` → `--copper-deep`
- Cards: 1px border `--moss` at 30% opacity, no shadow or very soft shadow
- Section dividers: thin 1px lines in `--moss` rather than heavy blocks

### Tech stack

- **Astro + Tailwind CSS** (static export)
- **GSAP** for the 3-agent trust loop animation (centerpiece)
- **Lenis** for smooth scroll (optional)
- **Deployment:** Cloudflare Pages (recommended) — free, fast, free subdomain

### Site structure (single long page)

1. **Hero** — "Three agents. One enclave. No one sees your work." + CTA
2. **The problem** — side-by-side: today (broken trust) vs. VerdantForged (attested execution)
3. **The solution** — three roles, one verifiable pipeline (animated 3-agent diagram)
4. **How it works** — 5 steps from SPEC, each full-bleed with screen recording
5. **Live demo** — asciinema cast (3 min) embedded, autoplay on scroll
6. **Security** — SEV-SNP attestation, Stripe PaymentIntent verify-then-capture, E2E encryption comparison table
7. **Built on** — AMD SEV-SNP worker substrate, NemoClaw sandbox path, Stripe ACS/SPT + PaymentIntent, Hermes Agent (logo row, grayscale — sourced from official brand kits with attribution; pending kit audit, fallback to text-only)
8. **CTA** — "Try it in your agent" + `.Agent.md` download + one-click "Send to Hermes" link

### Proposed directory layout

```
~/hermes/competition/
├── tee-broker-pattern/      ← Rust core (DON'T TOUCH)
├── tee-broker-docs-archive-2026-06-29/         ← existing markdown docs (DON'T TOUCH)
├── tee-broker-site/         ← NEW: marketing site (this proposal)
│   ├── astro.config.mjs
│   ├── tailwind.config.ts
│   ├── package.json
│   ├── public/
│   │   ├── hero-poster.webp
│   │   ├── demo.cast         ← asciinema cast (autoplay embed)
│   │   ├── .well-known/
│   │   │   └── agent.json    ← A2A / agent discovery manifest
│   │   └── logos/...
│   ├── src/
│   │   ├── pages/index.astro
│   │   ├── pages/api/
│   │   │   └── agent-init.ts ← POST endpoint: human clicks → spawn Hermes run
│   │   ├── components/
│   │   │   ├── Hero.astro            ← type-driven, NO imagery
│   │   │   ├── TrustLoop.astro       ← 3-agent GSAP animation
│   │   │   ├── HowItWorks.astro
│   │   │   ├── DemoEmbed.astro       ← asciinema player
│   │   │   ├── SecurityTable.astro
│   │   │   ├── TryInAgent.astro      ← the meta-pitch CTA
│   │   │   └── Footer.astro
│   │   └── styles/global.css
│   ├── content/copy.md              ← all marketing copy, easy to edit
│   ├── .Agent.md                     ← ROOT: agent-runnable instruction file
│   ├── AGENT.md                      ← mirror (some agents look for this exact name)
│   └── README.md                     ← human-readable intro, links to .Agent.md
└── staging/                 ← ignored (leftover from prior project)
```

### Build order (≈13 hours total)

1. Scaffold Astro + Tailwind (1h)
2. Write `content/copy.md` — all marketing copy in one file (2h) — bottleneck, do this first
3. Hero + section layout (3h) — get the typographic rhythm down (TYPE-DRIVEN hero, no imagery)
4. 3-agent trust loop animation with GSAP (3h) — centerpiece
5. asciinema demo embed (1h)
6. Security table + footer + CTA (2h)
7. Write `.Agent.md` + AGENT.md + `TryInAgent.astro` component (2h) — meta-pitch CTA
8. Deploy to Cloudflare Pages (1h)

### The meta-pitch: try it in your agent (LOCKED 2026-06-19)

The site's CTA isn't "Read the spec" or "Try the sandbox" — it's **"Try it in your agent."** This sells the TEE Broker pattern by demonstrating it: VerdantForged is itself an example of agent-to-agent skill transfer.

**The flow:**
1. Human reads the marketing page, gets to the bottom
2. Sees a `TryInAgent.astro` panel: "Send this skill to your Hermes agent"
3. Three entry points, in order of friction:
   - **One-click "Send to Hermes"** — POSTs to `/api/agent-init`, server-side spawns a Hermes skill-install task against the user's connected agent (requires auth handshake — likely out of scope for hackathon)
   - **Copy-paste a Hermes prompt** — a `<textarea>` with the exact text the user pastes into their Hermes chat: *"Install the VerdantForged skill from ~/hermes/competition/tee-broker-site/.Agent.md and walk me through what it does."* One click to copy.
   - **Raw `.Agent.md` download** — visible button, downloads the file directly for users with their own agent runtime

**`.Agent.md` format (autumn's requirement):**
- First line: agent-runnable YAML frontmatter (Hermes skill discovery format: `name`, `description`, `tags`, `triggers`)
- Body: human-readable walkthrough of the VerdantForged protocol, with embedded commands the agent can execute (e.g. `cargo test --manifest-path ../tee-broker-pattern/Cargo.toml`)
- Designed to be **agent-first, human-readable-second**: a Hermes agent reading it should be able to verify the build, run the demo, and explain the protocol in <2 minutes

**Hidden `.Agent.md` at the site root:** agents crawling `verdantforged.market/.Agent.md` or `verdantforged.market/AGENT.md` get the file directly. Add `<link rel="alternate" type="text/markdown" href="/AGENT.md" title="Agent instructions">` to the page head so discovery tools find it.

### Browser-agent idea — already on the kanban board

The "Hermes agent running in the browser" idea is its own project — out of scope for the hackathon-deadline marketing site. Two existing kanban cards already capture this:
- `t_762bc24e` — Build Hermes Mobile Web App with Local In-Browser LLM (status: blocked, web-dev profile missing)
- `t_8ea7a325` — duplicate, blocked after coder dispatcher crashed twice (pid not alive)

No new task needed; the work is already queued at `~/hermes/webagent/`.

### Open questions (awaiting Autumn's decisions)

1. **Name:** Graftwood confirmed pending uniqueness check ✓
2. **Hosting target:** Cloudflare Pages (recommended) / Vercel / Netlify / self-host on 192.168.4.x?
3. **Color accent:** muted green `#2d6a4f` (VerdantFamiliar) or pure chrome-and-black?
4. **Demo video:** existing recording, or new script + record?
5. **Hero animation style:** (a) abstract particle field, (b) literal 3-agent diagram, (c) type-driven Apple-keynote-style? (Recommended: c for hero, b further down the page)

---

## Reminder — what this is NOT

- Not a redesign of the TEE Broker protocol
- Not a new Rust crate
- Not a replacement for tee-broker-docs-archive-2026-06-29/
- The 5-crate, 46-test Rust core stays untouched
