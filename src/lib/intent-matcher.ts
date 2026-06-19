/**
 * intent-matcher.ts — match free-text user intent to skill catalog entries.
 *
 * The dumb client doesn't have an LLM. It uses simple keyword routing.
 * Real-world: the broker's planner LLM (Hermes-3-8B inside the enclave)
 * does this with much higher accuracy. For the demo, the mock shows
 * what that planner WOULD emit given a representative intent.
 *
 * This is intentionally simple — the point is to show the *protocol*,
 * not to compete with a real planner LLM.
 */

import { SKILL_CATALOG, type SkillCatalogEntry } from './skill-catalog';

export interface MatchResult {
  primary: SkillCatalogEntry;
  followups: SkillCatalogEntry[]; // 0–2 additional steps
  reason: string;
}

const RULES: Array<{ test: RegExp; skill: string; followups?: string[]; reason: string }> = [
  {
    test: /\b(review|audit|check|inspect|find bugs|vulnerabilit)\b/i,
    skill: 'code-review-v3.2',
    followups: ['summarize-doc'],
    reason: 'user asked to review/audit code',
  },
  {
    test: /\b(test|tests|pytest|unittest|coverage)\b/i,
    skill: 'generate-tests',
    followups: ['summarize-doc'],
    reason: 'user asked for tests',
  },
  {
    test: /\b(translate|french|spanish|german|fr|en|es|de)\b/i,
    skill: 'translate-fr',
    followups: [],
    reason: 'translation request',
  },
  {
    test: /\b(sql|query|database|select|where)\b/i,
    skill: 'sql-query',
    followups: ['summarize-doc'],
    reason: 'SQL query request',
  },
  {
    test: /\b(image|caption|picture|describe this)\b/i,
    skill: 'image-caption',
    followups: [],
    reason: 'image caption request',
  },
  {
    test: /\b(pdf|extract|parse|document)\b/i,
    skill: 'extract-pdf-text',
    followups: ['summarize-doc'],
    reason: 'PDF extraction request',
  },
  {
    test: /\b(summarize|tl;dr|summary|short version)\b/i,
    skill: 'summarize-doc',
    followups: [],
    reason: 'summarization request',
  },
  {
    test: /\b(compute|calculate|fibonacci|fib|math)\b/i,
    skill: 'code-review-v3.2', // use code-review as the demo default for math
    followups: [],
    reason: 'computation — defaulting to code-review sandbox',
  },
  {
    test: /\b(help|what can|capabilities|features)\b/i,
    skill: 'summarize-doc',
    followups: [],
    reason: 'capability inquiry — pointing at summarize-doc as entry point',
  },
];

/**
 * Pick skills for the user's intent.
 *
 * Strategy:
 *   1. First matching rule wins for the primary skill.
 *   2. Followups from that rule are added.
 *   3. If no rule matches, default to summarize-doc.
 *
 * Returns the chosen primary + 0–2 followups.
 */
export function matchIntentToSkills(intent: string): MatchResult {
  const text = intent.trim().toLowerCase();
  if (!text) {
    return {
      primary: SKILL_CATALOG.find(s => s.name === 'summarize-doc')!,
      followups: [],
      reason: 'empty intent — defaulting to summarize-doc',
    };
  }

  for (const rule of RULES) {
    if (rule.test.test(text)) {
      const primary = SKILL_CATALOG.find(s => s.name === rule.skill);
      if (!primary) continue;
      const followups = (rule.followups ?? [])
        .map(name => SKILL_CATALOG.find(s => s.name === name))
        .filter((s): s is SkillCatalogEntry => Boolean(s))
        .slice(0, 2);
      return { primary, followups, reason: rule.reason };
    }
  }

  // No rule matched — default to code-review (most visually obvious result).
  return {
    primary: SKILL_CATALOG.find(s => s.name === 'code-review-v3.2')!,
    followups: [],
    reason: 'no specific match — defaulting to code-review-v3.2 (most practiced)',
  };
}

/**
 * Generate the result summary text for a step's completion.
 * Mock-only — the real executor would return real output.
 */
export function mockStepResult(skill: SkillCatalogEntry, intent: string): string {
  const lower = intent.toLowerCase();
  switch (skill.name) {
    case 'code-review-v3.2':
      if (lower.includes('fib')) {
        return 'fib(40) = 102334155. computed in 3 wasmtime steps.';
      }
      return 'Found 3 critical issues, 7 warnings, 12 style nits. Output below.';
    case 'summarize-doc':
      return 'Top concerns: unhandled exception in main, missing type hints, hardcoded credentials.';
    case 'generate-tests':
      return 'Generated 47 pytest cases. Coverage estimate: 84%.';
    case 'translate-fr':
      return 'Translation complete. Output: "Bonjour, le monde."';
    case 'sql-query':
      return 'Query executed against sandbox. Returned 12 rows.';
    case 'image-caption':
      return 'A verdant forest under soft morning light. 1102 prior runs inform this caption.';
    case 'extract-pdf-text':
      return 'Extracted 4 pages, 3 tables, 1,847 words.';
    default:
      return 'complete';
  }
}
