# Marketing-site copy grounded in deploy code — audit workflow

## Why this exists

The `tee-broker-site/` marketing site is the public face of the broker
deployed by `tee-broker-deploy/`. The site accumulates prose faster than
the deploy code is updated, and the drift is invisible until a user
pushes back. The drift falls into three classes:

1. **Architecture drift.** Wrong claims about where things run.
   Textbook case: I asserted in `/topology` section 1 that "the broker
   runs inside NemoClaw," and was wrong — `scripts/bootstrap-control-plane.sh`
   installs the daemon as a bare `systemd` service on the t3.small host.
   NemoClaw only appears at the **worker** side, as a per-LLM-job process
   inside the worker EC2, not a new instance.
2. **Protocol drift.** Wrong field names, env vars, lifecycle states.
   Examples that surfaced in the audit: `LLM_UPSTREAM_*` (actual:
   `BROKER_LLM_*`), `mpp_escrow_id` (actual: `stripe_pi_id`), "signed
   enclave teardown" (no such concept — `_finalize_job` captures on
   `completed` state), `AES-256-GCM` (actual: `x25519-hkdf-sha256-chacha20poly1305-v1`),
   "Rust control plane" (actual: Python daemon).
3. **URL drift.** Dead GitHub repos (`github.com/codepilots/tee-broker-pattern`
   returns 404, the repo doesn't exist on GitHub), dead
   `verdantforged.pages.dev/AGENT.md` (DNS doesn't resolve yet — that's
   the *planned* deploy target, intentional, but a `github.com/codepilots/...`
   link in a CTA is unambiguously wrong).

The fix is a one-pass audit. This file is the procedure.

## Step 1 — Read the source of truth

Before writing a single word of copy or running a single patch:

```
tee-broker-deploy/broker-daemon/daemon.py
  L3168-3250   discover endpoint (what /v1/discover returns)
  L2200-2230   _verify_snp_quote_signature (broker's own attestation check)
  L2233-2270   _load_verified_worker_identity (worker_binding HMAC, source filter)
  L2537        reference to attestation-verifier skill
  L2643        enclave_pubkey reference (clients encrypt to this)
  L2933-2946   result attestation block
  L3170-3248   discover attestation_block fields (tee_type, min_measurement,
               worker_attested, policy_hash, report, report_data, cert_chain,
               enclave_pubkey, chip_id, family_id, attestation_source)
  L3888        _finalize_job (capture vs refund vs topup)
  L3420-3550   submit_job envelope (encrypted_skill, encrypted_data,
               result_pubkey, requester_sig, stripe_pi_id)

tee-broker-deploy/worker/sev_snp.py
  entire file — TSM configfs + snpguest + chip_id/report_data parsing
  L39-156   _tsm_fetch_report (real SEV-SNP via /sys/kernel/config/tsm/report/)
  L158-215  _snpguest_fetch_report (fallback)
  L218-243  fetch_sev_snp_attestation (source-of-truth for the .attestation object)
  L246-279  get_sev_snp_measurement (IMDSv2 fallback when SEV-SNP unavailable)
  L282-299  get_full_attestation (the /v1/discover shape)

tee-broker-deploy/worker/poller.py
  L63        "deriving the signing seed from the SNP attestation report"
  L610-693   build_attestation_verdict (the attestation-verifier skill)
  L899-916   get_sev_snp_measurement
  L975-1007  NemoClaw sandbox name resolution
  L1028-1190 dispatch_to_sandbox (nemohermes exec invocation)
  L1577-1700 execute_in_envelope (WASM path + LLM path dispatch tree)
  L1857      get_sev_snp_measurement call site (in result envelope)
  L2080-2130 wasm_attestation block
  L2185      result attestation block (attested: true)
  L2228-2250 attestation-verifier skill early dispatch
  L2545-2580 execute_in_envelope precedence (WASM > tool-loop > NemoClaw > legacy proxy)
  L2608-2640 sandbox_attestation block (note: 4 fields, no version hash)
  L2709-2719 result["sandbox"] attachment

tee-broker-deploy/worker/user-data.sh
  L182-275   NemoClaw install + onboard + skill install

tee-broker-deploy/scripts/bootstrap-control-plane.sh
  L1-100     install python deps, nfs-common, caddy, awscli
  L100-160   copy daemon + openshell + caddyfile + worker bootstrap to EFS
  L160-200   STRIPE_SECRET_KEY fetch from SSM
  L200-300   generate config.env from CFN outputs
```

That's the source of truth. If a marketing claim contradicts anything
in those slices, the marketing is wrong.

## Step 2 — Drift-signal grep

Run this against the whole `tee-broker-site/` tree (src + public + AGENT.md
+ .well-known + rendered dist):

```bash
cd /home/autumn/hermes/competition/tee-broker-site

for kw in "MPP release" "MPP escrow" "mpp_escrow_id" "MPPEscrowID" \
          "magic payment" "MagicPayment" "ECIES" "AES-256-GCM" "aes-256-gcm" \
          "signed enclave teardown" "enclave teardown" "teardown receipt" \
          "no syscalls" "LLM_UPSTREAM" "Rust control plane" \
          "Skill Provider" "marketplace_protocol" "agent-marketplace" "x402" \
          "51/51 tests" "clippy" "2,080 LoC"; do
    c=$(grep -r "$kw" src/ public/ --include="*.html" --include="*.md" \
                                --include="*.astro" --include="*.json" 2>/dev/null | wc -l)
    printf "%-30s : %s\n" "$kw" "$c"
done
```

For URLs:

```bash
grep -rEn "github\.com/codepilots|github\.com/hermes-agent/verdant|verdantforged\.pages\.dev/.Agent\.md" \
        src/ public/ --include="*.html" --include="*.md" --include="*.astro" --include="*.json" 2>/dev/null
```

`verdantforged.pages.dev` itself is NOT a drift signal — it's the
planned deploy target in `astro.config.mjs` and `wrangler.toml`, and
appears ~24 times in the rendered `dist/` for canonical URL tags. Only
flag it if it's pointing at a subpath that was never deployed (e.g.
`/AGENT.md` resolves at the live broker's `verdant.codepilots.co.uk/AGENT.md`
but not at the pages.dev hostname yet — that's a deploy-state issue,
not drift).

## Step 3 — Fix order

1. **`src/components/*.astro`** — visible sections (Hero, Pillars,
   HowItWorks, SecurityTable, etc.). These are what a casual visitor
   sees first. Fix drift here first.
2. **`src/pages/*.astro`** — deep-dive pages. Same drift, more detail.
3. **`public/AGENT.md` and `public/.well-known/agent.json`** —
   third-party-facing artifacts (A2A discovery, LLM agent onboarding).
4. **`astro.config.mjs` and `wrangler.toml`** — canonical URLs, base path.
5. **Root `AGENT.md`** — the canonical agent doc. Re-sync into
   `public/AGENT.md` and `public/.Agent.md` (one source of truth, three
   copies).

## Step 4 — Build and verify

```bash
cd /home/autumn/hermes/competition/tee-broker-site
./node_modules/.bin/astro build

# Re-run the drift grep against dist/
for kw in "MPP release" "mpp_escrow_id" "AES-256-GCM" "teardown receipt" \
          "no syscalls" "LLM_UPSTREAM" "Rust control plane" "x402"; do
    c=$(grep -r "$kw" dist/ --include="*.html" --include="*.md" \
                       --include="*.json" 2>/dev/null | wc -l)
    printf "%-30s : %s\n" "$kw" "$c"
done
```

The rendered output can drift from `src/` because Astro escapes some
strings into HTML entities. If `src/` is clean and `dist/` shows hits,
inspect the rendered HTML to see whether the escaped form is still
inaccurate (e.g. `&quot;` around a stale string).

## Step 5 — Preserve the change as a docstring

When removing a stale claim from a section, leave a one-line comment in
the Astro frontmatter saying what was removed and why. The next editor
needs the historical context to know it was a deliberate fix, not an
oversight. Pattern:

```js
/**
 * Updated 2026-06-30: removed the "Rust core" framing — the live broker
 * is Python. The Rust pattern in tee-broker-pattern/ uses 5 attestation
 * checks; the live broker uses 6 checks against the 1184-byte SNP
 * report (see worker/poller.py get_sev_snp_measurement).
 */
```

## Drift-fix precedents in this project

### Round 1 (2026-06-29, partial)

- 7 components fixed: HowItWorks, Pillars, SecurityTable, DemoEmbed,
  TryInAgent, Footer, SiteNav
- 9 pages fixed: index, attestation, sandboxing, payment, security,
  terms, pricing, payment-flow, quickstart, agents, docs
- 3 static files: AGENT.md, public/AGENT.md, public/.Agent.md
- 1 skill updated: hermes-terminal-secret-redaction v1.2 → v1.3 (added
  the "stop and escalate" rule)

### Round 2 (2026-06-30, this audit)

- 2 new pages: `/verify-attestation/` and `/topology/`
- 1 page rewritten: `/topology/` to fix the "broker in NemoClaw" claim
  after user pushback
- 1 new design sketched (not yet implemented): signed NemoClaw sandbox
  attestation — see `/topology` section 3

## Three pitfalls to remember

### Pattern-fill from memory

The most common drift is invented plausible-sounding architecture
prose. The fix is always to **read the code before writing the copy**,
never to write copy and then check the code. The order matters:
grounding first, then writing.

### "NemoClaw instance" ≠ a separate EC2

NemoClaw in this design is a per-LLM-job **process** inside the worker
EC2, spawned by `nemohermes exec`. It is not a new instance, not a new
VM, not a new hardware attestation boundary. It inherits the worker's
SEV-SNP attestation. The result envelope adds a `sandbox` block
(`worker/poller.py:2611`) that asserts the network surface but does not
pin the NemoClaw version or sign anything.

### Planned deploy URL is not a dead link

`verdantforged.pages.dev` shows up in `dist/` ~24 times because it IS
the planned deploy target per `astro.config.mjs` and `wrangler.toml`.
It is not drift; it is the intended production URL. Filter it out of
the drift-grep unless you're specifically checking that the config
files agree.
