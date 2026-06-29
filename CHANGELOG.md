# Changelog

All notable changes to the VerdantForged site.

## [Unreleased]
- **Pivot (2026-06-29):** the site is now broker-as-product, not marketplace. Removed Provider / Requester framing, the three-agent TrustLoop, the in-browser Pyodide splash (BrowserAgentSplash), the marketplace SkillShowcase, and the session dashboard tile. Added the four-pillar centerpiece (Attestation, Security, Sandboxing, Payment) with audit-anchored "what stops what" tables for each pillar.
- **New section: Pillars.astro** — Attestation (SEV-SNP measurement, TCB, replay protection) · Security (ECIES, Ed25519, ephemeral keys) · Sandboxing (wasmtime, fuel limits, default-DENY network) · Payment (Stripe MPP escrow tied to signed teardown receipt).
- **SecurityTable.astro deepened** — now lists the actual crypto primitives (X25519, AES-256-GCM, HKDF-SHA256, Ed25519, SHA-256) and the specific test names that prove each property, plus the ExecutionRequest validation table and the 6-check attestation verification table.
- **Hero rewritten** — "The broker that runs your agent inside a hardware-attested enclave" + "Attestation. Security. Sandboxing. Payment." subhead. CTAs reordered: See the four pillars / Read the security audit / Try in your agent.
- **TryInAgent.astro** — Pyodide / "Try in browser" entry point removed. Replaced with "Read the spec + audit" as the primary path, paste-prompt kept, direct download kept.
- **Footer voice updated** — broker-as-product, no longer describes a marketplace.
- **BaseLayout defaults** — title and description updated to broker-as-product.

## [Previous]
- Added Stripe chargeback and dispute clause to Terms of Service.
- Added cross-link from Payment Flow documentation to dispute terms.
