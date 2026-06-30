/**
 * skill-catalog.ts — the 7 skills the demo marketplace knows about.
 *
 * Each entry has the realistic shape of a Nostr Kind 31989 announcement
 * (see tee-broker-docs-archive-2026-06-29/discovery/DISCOVERY.md). The values are synthetic
 * for the demo, but the structure is what a real provider would publish.
 *
 * The variety (different envs, different cost ranges, different n=)
 * is what makes the §9 cost-prediction visualization interesting.
 *
 * NOTE: Amounts are scaled to dollars (not cents) — Stripe's sandbox
 * minimum is ~$3.20 (must convert to ≥30 pence), so all skill prices
 * are above that floor. Real marketplace economics would be in the
 * $0.01–$0.10 range; the demo values are 1000× higher to satisfy the
 * sandbox floor while keeping the per-step economics visible.
 */

export interface SkillCatalogEntry {
  name: string;
  description: string;
  env: 'python-3.11-base' | 'python-3.11-data-science' | 'node-20-base';
  static_cost_usd: number;
  /** How many times this skill has been executed historically. Drives
   *  the predicted-cost CI and the "practiced" badge. */
  n_executions: number;
  /** 1–5 star reputation, half-stars ok. */
  reputation: number;
  /** Stripe Connect account that receives the per-step transfer. */
  provider_acct: string;
  /** Display name shown in the receipts view. */
  provider_name: string;
  /** Default args when the planner picks this skill. */
  default_args: Record<string, unknown>;
  /** One-line description of why this skill exists. */
  why_template: (intent: string) => string;
}

export const SKILL_CATALOG: ReadonlyArray<SkillCatalogEntry> = [
  // NOTE: These account IDs are currently synthetic placeholders.
  // They should be replaced with real Stripe Connect account IDs from the Stripe dashboard.
  // See task t_cae20aa4 for details on setting up real Connect accounts.
  {
    name: 'code-review-v3.2',
    description: 'Security-focused code review for Python/JS/Rust.',
    env: 'python-3.11-base',
    static_cost_usd: 24.00,
    n_executions: 247,
    reputation: 4.8,
    provider_acct: 'acct_1QAcmPLM5nrG2kAc',
    provider_name: 'Acme Code Review Inc.',
    default_args: { target_path: '/sandbox/user_input.py' },
    why_template: () => 'code review skill — practiced 247 times, predictable cost',
  },
  {
    name: 'summarize-doc',
    description: 'Section-aware summarization of loaded documents.',
    env: 'python-3.11-base',
    static_cost_usd: 8.00,
    n_executions: 891,
    reputation: 4.9,
    provider_acct: 'acct_1QBdocRN9xvK3mPz',
    provider_name: 'DocDigest Labs',
    default_args: { query: 'summarize the loaded docs' },
    why_template: () => 'summarize the result for the user',
  },
  {
    name: 'generate-tests',
    description: 'Generate pytest test suite from source code.',
    env: 'python-3.11-data-science',
    static_cost_usd: 41.00,
    n_executions: 156,
    reputation: 4.3,
    provider_acct: 'acct_1QCtstVW4qLm8nRs',
    provider_name: 'TestForge Studio',
    default_args: { source_path: '/sandbox/user_input.py', framework: 'pytest' },
    why_template: () => 'generate pytest coverage for the input',
  },
  {
    name: 'translate-fr',
    description: 'Multilingual translation, optimized for Romance languages.',
    env: 'node-20-base',
    static_cost_usd: 12.00,
    n_executions: 432,
    reputation: 4.7,
    provider_acct: 'acct_1QDtrlHQ2bnF7wYx',
    provider_name: 'Polyglot AI',
    default_args: { target_lang: 'fr', preserve_formatting: true },
    why_template: () => 'translation request — French target',
  },
  {
    name: 'sql-query',
    description: 'Safe SQL generation + execution against a sandboxed DB.',
    env: 'python-3.11-data-science',
    static_cost_usd: 29.00,
    n_executions: 78,
    reputation: 4.2,
    provider_acct: 'acct_1QEsqlME3pkT9vBn',
    provider_name: 'QueryWell',
    default_args: { dialect: 'postgres', read_only: true },
    why_template: () => 'SQL query — sandboxed DB execution',
  },
  {
    name: 'image-caption',
    description: 'Vision-language captioning with detail level control.',
    env: 'python-3.11-base',
    static_cost_usd: 18.00,
    n_executions: 1102,
    reputation: 4.9,
    provider_acct: 'acct_1QFimgCP8hwR6jZs',
    provider_name: 'CaptionCorp',
    default_args: { detail: 'thorough' },
    why_template: () => 'caption image — practiced 1102 times',
  },
  {
    name: 'extract-pdf-text',
    description: 'Layout-aware PDF text extraction with table recovery.',
    env: 'python-3.11-base',
    static_cost_usd: 6.00,
    n_executions: 334,
    reputation: 4.6,
    provider_acct: 'acct_1QGpdfXT5yrD4kLq',
    provider_name: 'ParsePerfect',
    default_args: { source: 'user_upload', tables: true },
    why_template: () => 'PDF extraction — cheapest practiced skill',
  },
];

// ─── Broker's own platform account (for the application_fee skim) ───

export const BROKER_PLATFORM_ACCT = 'acct_1QBrokerPLATFORM001';
export const BROKER_PLATFORM_NAME = 'VerdantForged Broker';

/**
 * Predict cost range from static + n_executions.
 * More executions → tighter CI (the §9 ML estimator concept).
 * Stable per (skill, session) so the visual is consistent during a session.
 */
export function predictedRange(static_cost_usd: number, n_executions: number, seed = 0): { low: number; high: number; n: number } {
  // n < 10 → wide CI (cold start, fall back to static only per §9.4)
  // n >= 100 → tight CI (well-practiced)
  // Use log scale: width = 0.30 * static when n=10, 0.12 * static when n=1000
  const width = n_executions < 10
    ? 0.45
    : n_executions < 100
      ? 0.30
      : n_executions < 500
        ? 0.18
        : 0.12;
  const jitter = (seed % 7) / 100; // tiny per-session variation
  return {
    low: Math.max(0.001, +(static_cost_usd * (1 - width + jitter)).toFixed(4)),
    high: +(static_cost_usd * (1 + width - jitter)).toFixed(4),
    n: n_executions,
  };
}
