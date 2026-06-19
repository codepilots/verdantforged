/**
 * verdant-agent.ts — wires the live SessionDashboard "Make skill call" button
 * to a real Hermes-pyodide runtime running the `verdantforged.explainer`
 * skill (a cutdown from the 7-skill portable-hermes set in
 * ~/hermes/competition/tee-broker-spike/skills/portable_hermes/).
 *
 * What this is NOT:
 *   - The full hermes-pyodide runtime (600KB+). We load only what we need.
 *   - A remote inference path. Everything runs locally in the browser.
 *   - A billing integration. Costs are calculated from in-tab tokens.
 *
 * What it IS:
 *   - A single shared Pyodide singleton on `window.__vfPyodide`, loaded once
 *     and reused across both dashboards on the same page.
 *   - One real skill: `verdantforged.explainer.summarize_section()` which
 *     fetches AGENT.md, picks a random `## Section` heading, and returns
 *     a 1-paragraph plaintext summary with token + LLM-cost accounting.
 *   - Visible progress in the dashboard log: "running…" → "ok".
 *
 * The Nous inference model is mocked locally because the demo runs entirely
 * client-side (no API key shipping in the bundle). In production this is
 * where `inference-api.nousresearch.com/v1/chat/completions` gets called —
 * see tee-broker-pattern/src/skills/code_review.rs for the real wire.
 */

const PYODIDE_VERSION = 'v0.27.7';
const PYODIDE_CDN = `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`;

/**
 * Derive the project's base URL from the page's script tags.
 *
 * Astro emits <script type="module" src="/_astro/foo.js"> for client-side
 * bundles. If base='/verdantforged' is configured, the script src becomes
 * '/verdantforged/_astro/foo.js'. We extract the prefix and use it for
 * fetching AGENT.md and other project-root files.
 *
 * Works for both:
 *   - GitHub Pages at /verdantforged/  → returns '/verdantforged/'
 *   - Cloudflare Pages at /            → returns '/'
 */
function resolveBaseUrl(): string {
  // First try: any script with /_astro/ in its src tells us the base.
  const astroScript = document.querySelector<HTMLScriptElement>(
    'script[src*="/_astro/"]',
  );
  if (astroScript?.src) {
    try {
      const u = new URL(astroScript.src, location.href);
      const idx = u.pathname.indexOf('/_astro/');
      if (idx >= 0) return u.pathname.slice(0, idx + 1);  // keep trailing slash
    } catch {
      // fall through
    }
  }
  // Fallback: location.pathname minus the index.html filename
  const p = location.pathname;
  const lastSlash = p.lastIndexOf('/');
  if (lastSlash > 0) return p.slice(0, lastSlash + 1);
  return '/';
}

// Singleton state on window. Survives across multiple createDashboard()
// calls on the same page (landing-page tile + splash tile share).
declare global {
  interface Window {
    __vfPyodide?: Promise<any>;             // shared Pyodide load promise
    __vfPyodideReady?: boolean;             // true once `runPythonAsync` available
    __vfAgentSkill?: any;                   // the loaded Python module
    __vfAgentMd?: string;                   // cached AGENT.md content
    loadPyodide?: (opts: { indexURL: string }) => Promise<any>;
  }
}

/**
 * Lazily load Pyodide + register the VerdantForged skill. The promise
 * resolves to a callable `skillCall()` function the dashboard can await.
 *
 * Idempotent: subsequent calls return the same promise; Pyodide is loaded
 * only once per page.
 */
export async function loadVerdantAgent(opts?: {
  /** Override AGENT.md URL (defaults to `${BASE_URL}/AGENT.md`). */
  agentMdUrl?: string;
  /** Override CDN index URL (defaults to jsdelivr Pyodide v0.27.7). */
  pyodideIndex?: string;
}): Promise<() => Promise<SkillResult>> {
  const indexURL = opts?.pyodideIndex ?? PYODIDE_CDN;

  if (!window.__vfPyodide) {
    window.__vfPyodide = (async () => {
      // Inject the Pyodide loader script tag (idempotent — check first).
      const existing = document.querySelector(
        `script[data-vf-pyodide-loader]`,
      );
      if (!existing) {
        const s = document.createElement('script');
        s.src = `${indexURL}pyodide.js`;
        s.dataset.vfPyodideLoader = 'true';
        document.head.appendChild(s);
        await new Promise<void>((resolve, reject) => {
          s.onload = () => resolve();
          s.onerror = () =>
            reject(
              new Error(
                `Failed to load Pyodide from ${indexURL}pyodide.js. ` +
                  `Check your network or use the "Paste into chat" option.`,
              ),
            );
        });
      }
      if (!window.loadPyodide) {
        throw new Error('Pyodide loader script loaded but window.loadPyodide missing.');
      }
      const pyodide = await window.loadPyodide({ indexURL });

      // Bootstrap the VerdantForged skill. This is the cutdown version of
      // portable_hermes/verdantforged_explainer.py from the spike — only
      // the summarize_section entry point. Full skill catalog registers
      // 7 skills; we ship 1 for the demo to keep the bundle ~20KB.
      // Resolve AGENT.md relative to where this module's <script> was
      // loaded from. import.meta.env.BASE_URL is build-time only and
      // not visible at runtime in the browser, so we sniff
      // document.currentScript or derive from location.pathname.
      const baseUrl = resolveBaseUrl();
      const agentMdUrl = opts?.agentMdUrl ?? `${baseUrl}AGENT.md`;

      await pyodide.runPythonAsync(BOOTSTRAP_PY);

      // Fetch AGENT.md and hand it to Python as a string. Cached so
      // repeated calls don't re-hit the network.
      if (!window.__vfAgentMd) {
        const r = await fetch(agentMdUrl);
        if (!r.ok) {
          throw new Error(`Failed to fetch AGENT.md (HTTP ${r.status})`);
        }
        window.__vfAgentMd = await r.text();
      }
      pyodide.globals.set('agent_md_text', window.__vfAgentMd);
      await pyodide.runPythonAsync('verdantforged.explainer.load(agent_md_text)');

      window.__vfAgentSkill = pyodide;
      window.__vfPyodideReady = true;
      return pyodide;
    })();
  }

  await window.__vfPyodide;
  return skillCall;
}

export type SkillResult = {
  /** Short human-readable summary string for the dashboard log. */
  summary: string;
  /** Cost in USD for this invocation's Nous inference. */
  llmCost: number;
  /** Total tokens used (input + output), for the log suffix. */
  tokens: number;
};

/**
 * Run one skill call: pick a random section of AGENT.md, summarize it,
 * compute a fake-but-plausible LLM cost.
 *
 * Mock cost model: $0.000003 per token (roughly Nous Hermes-4 70B list
 * price). Tokens are approximated by `len(text) / 4` for input and
 * `len(summary) / 4` for output. Real production path would call
 * inference-api.nousresearch.com and use the actual token counts.
 */
async function skillCall(): Promise<SkillResult> {
  const pyodide = window.__vfAgentSkill;
  if (!pyodide) {
    throw new Error('Pyodide skill not loaded. Call loadVerdantAgent() first.');
  }
  // Cycle through sections deterministically (callCount isn't passed in,
  // but Python keeps a counter on the module side).
  const summary = await pyodide.runPythonAsync(
    'verdantforged.explainer.summarize_next()',
  );
  const tokensIn = await pyodide.runPythonAsync(
    'verdantforged.explainer.last_input_tokens()',
  );
  const tokensOut = await pyodide.runPythonAsync(
    'verdantforged.explainer.last_output_tokens()',
  );
  const totalTokens = Number(tokensIn) + Number(tokensOut);
  // Roughly aligned with Nous Portal's published rates for Hermes-4-70B.
  const llmCost = totalTokens * 0.000003;
  return {
    summary: String(summary),
    llmCost,
    tokens: totalTokens,
  };
}

/**
 * Returns a DashboardHandle-ready onSkillCall that lazily loads the
 * agent on the first invocation. Subsequent calls reuse the singleton.
 *
 * Safe to pass directly to createDashboard({ onSkillCall }).
 */
export function makeOnSkillCall(opts?: {
  agentMdUrl?: string;
  pyodideIndex?: string;
}): () => Promise<SkillResult> {
  let ready: Promise<() => Promise<SkillResult>> | null = null;
  return async () => {
    if (!ready) ready = loadVerdantAgent(opts);
    const call = await ready;
    return call();
  };
}

/**
 * Inline Python that registers `verdantforged.explainer` with Pyodide.
 *
 * This is the *only* Python in the bundle. ~30 lines, parses AGENT.md,
 * tracks a section index, summarizes one section per call. No external
 * imports — stdlib only (re, textwrap).
 */
const BOOTSTRAP_PY = `
import re, sys, textwrap

class _Explainer:
    """verdantforged.explainer — cutdown of portable_hermes skill set.

    Real catalog: lease_session, find_skills, broker_call, verify_attestation,
    pay_with_stripe, dashboard, verdantforged_explainer. We ship only this
    one for the in-browser demo; the full set is on the server side and
    requires authenticated Stripe + NemoClaw credentials.
    """
    def __init__(self):
        self.sections = []          # [(heading, body_paragraphs)]
        self.cursor = 0             # round-robin index
        self._last_in = 0
        self._last_out = 0

    def load(self, agent_md: str) -> None:
        """Parse AGENT.md into (heading, body) sections."""
        out = []
        current = None
        for line in agent_md.split('\\n'):
            if line.startswith('## '):
                if current is not None:
                    out.append(current)
                current = (line[3:].strip(), [])
            elif current is not None and line.strip() and not line.startswith('#'):
                current[1].append(line.strip())
        if current is not None:
            out.append(current)
        # Filter empty sections
        self.sections = [(h, b) for h, b in out if b]
        self.cursor = 0
        sys.stderr.write(f"[verdantforged.explainer] loaded {len(self.sections)} sections\\n")

    def _pick(self):
        if not self.sections:
            return ('(empty)', 'No sections found in AGENT.md.')
        h, body = self.sections[self.cursor % len(self.sections)]
        self.cursor += 1
        return h, ' '.join(body)

    def summarize_next(self) -> str:
        heading, body = self._pick()
        # First sentence + 1 follow-up if available. Cap at 240 chars.
        first = body.split('. ')
        summary = first[0]
        if len(first) > 1 and len(summary) < 160:
            summary += '. ' + first[1].split('. ')[0]
        if len(summary) > 240:
            summary = summary[:237] + '…'
        # Token accounting (rough: 1 token ≈ 4 chars)
        self._last_in = len(body) // 4
        self._last_out = len(summary) // 4
        return f"§ {heading} — {summary}"

    def summarize_query(self, query: str) -> str:
        """Match a free-form prompt against AGENT.md sections.

        Scoring (rough but deterministic):
          +5  if any keyword from the query appears in the heading
          +1  per keyword found in the body
          +2  bonus if the heading starts with a keyword
          +3  bonus if a multi-word phrase from the query is contiguous

        Returns the highest-scoring section, or the round-robin pick
        if nothing scores above zero. The "no good match" fallback is
        intentional — the dashboard still needs to surface SOMETHING
        so the user knows the call completed.
        """
        if not query or not query.strip() or not self.sections:
            return self.summarize_next()
        # Tokenize query: lowercase, split on whitespace + punctuation
        import re as _re
        words = [_re.sub(r'[^a-z0-9]', '', w) for w in query.lower().split()]
        words = [w for w in words if len(w) >= 3]
        if not words:
            return self.summarize_next()
        phrases = []
        for i in range(len(words) - 1):
            phrases.append(words[i] + ' ' + words[i + 1])

        best = None
        best_score = 0
        for h, body in self.sections:
            hl = h.lower()
            body_text = ' '.join(body).lower()
            score = 0
            for w in words:
                if w in hl:
                    score += 5
                    if hl.startswith(w):
                        score += 2
                body_hits = body_text.count(w)
                score += min(body_hits, 3)  # cap per-word body hits
            for ph in phrases:
                if ph in body_text:
                    score += 3
            if score > best_score:
                best = (h, body)
                best_score = score

        if best is None:
            return self.summarize_next()
        h, body = best
        # Compose the answer: heading + first 2 sentences of body
        joined = ' '.join(body)
        sentences = joined.split('. ')
        summary = '. '.join(sentences[:2]).strip()
        if not summary.endswith('.'):
            summary += '.'
        if len(summary) > 320:
            summary = summary[:317] + '…'
        # Bump cursor to this position so subsequent round-robin picks
        # continue from where the user just looked.
        try:
            self.cursor = self.sections.index(best) + 1
        except ValueError:
            pass
        # Token accounting
        self._last_in = (len(query) + len(joined)) // 4
        self._last_out = len(summary) // 4
        return f"§ {h} — {summary}"

    def last_input_tokens(self) -> int:
        return self._last_in

    def last_output_tokens(self) -> int:
        return self._last_out

# Register the skill on a stable module path
import types
verdantforged = types.ModuleType('verdantforged')
explainer = _Explainer()
verdantforged.explainer = explainer
sys.modules['verdantforged'] = verdantforged
sys.modules['verdantforged.explainer'] = explainer
`;
