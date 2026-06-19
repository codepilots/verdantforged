/**
 * portable-hermes.ts — Pyodide singleton + Hermes Portable Python skill.
 *
 * Replaces the old "explainer" mock with a real Hermes Portable runtime
 * that exposes skills to the in-browser LLM via WebLLM's OpenAI-shaped
 * chat.completions API.
 *
 * Skills shipped in this cutdown:
 *   1. broker_session — open/extend/close a VerdantForged broker session.
 *                        This is the skill the user requested: Hermes
 *                        decides when to invoke it, not the page UI.
 *   2. summarize      — answer questions about the loaded docs
 *                        (AGENT.md, etc.) by section match.
 *   3. list_skills    — return the skill catalog to the LLM so it can
 *                        decide which to call.
 *
 * The Python skill emits a custom DOM event ('vf:broker-session') that
 * the JS dashboard listens for. The dashboard handles the actual
 * session lifecycle (cost tracking, lease countdown) — Python just
 * signals intent.
 */

import { loadWebLLM, getEngineState } from './webllm-engine';

const PYODIDE_VERSION = 'v0.27.7';
const PYODIDE_CDN = `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`;

/**
 * Derive the project's base URL from the page's script tags.
 * (Same trick as verdant-agent.ts — copied to keep modules independent.)
 */
function resolveBaseUrl(): string {
  const astroScript = document.querySelector<HTMLScriptElement>(
    'script[src*="/_astro/"]',
  );
  if (astroScript?.src) {
    try {
      const u = new URL(astroScript.src, location.href);
      const idx = u.pathname.indexOf('/_astro/');
      if (idx >= 0) return u.pathname.slice(0, idx + 1);
    } catch { /* fall through */ }
  }
  const p = location.pathname;
  const lastSlash = p.lastIndexOf('/');
  if (lastSlash > 0) return p.slice(0, lastSlash + 1);
  return '/';
}

declare global {
  interface Window {
    __vfPyodide?: Promise<any>;
    __vfPortableSkill?: any;
    loadPyodide?: (opts: { indexURL: string }) => Promise<any>;
  }
}

/**
 * Boot Hermes Portable: Pyodide + Python skills + the WebLLM shim.
 * Returns a handle exposing the chat function and skill catalog.
 */
export async function loadPortableHermes(opts?: {
  agentMdUrl?: string;
  pyodideIndex?: string;
  /** When true, skip the WebLLM dependency and use the mock fallback. */
  skipLlm?: boolean;
}): Promise<PortableHermesHandle> {
  const indexURL = opts?.pyodideIndex ?? PYODIDE_CDN;

  if (!window.__vfPyodide) {
    window.__vfPyodide = (async () => {
      const existing = document.querySelector('script[data-vf-pyodide-loader]');
      if (!existing) {
        const s = document.createElement('script');
        s.src = `${indexURL}pyodide.js`;
        s.dataset.vfPyodideLoader = 'true';
        document.head.appendChild(s);
        await new Promise<void>((resolve, reject) => {
          s.onload = () => resolve();
          s.onerror = () => reject(new Error(
            `Failed to load Pyodide from ${indexURL}pyodide.js`,
          ));
        });
      }
      if (!window.loadPyodide) {
        throw new Error('Pyodide loader attached but window.loadPyodide missing.');
      }
      const pyodide = await window.loadPyodide({ indexURL });

      const baseUrl = resolveBaseUrl();
      const agentMdUrl = opts?.agentMdUrl ?? `${baseUrl}AGENT.md`;

      await pyodide.runPythonAsync(BOOTSTRAP_PY);

      // Hand AGENT.md to Python.
      const r = await fetch(agentMdUrl);
      if (!r.ok) throw new Error(`Failed to fetch AGENT.md (HTTP ${r.status})`);
      const agentMd = await r.text();
      pyodide.globals.set('agent_md_text', agentMd);
      await pyodide.runPythonAsync('verdantforged.portable.load(agent_md_text)');

      window.__vfPortableSkill = pyodide;
      return pyodide;
    })();
  }

  const pyodide = await window.__vfPyodide;

  return new PortableHermesHandle(pyodide, !!opts?.skipLlm);
}

export type ChatMessage = { role: 'system' | 'user' | 'assistant' | 'tool'; content: string };
export type ChatResult = {
  /** Final assistant message content. */
  content: string;
  /** Tool calls Hermes made during the turn (if any). */
  toolCalls: Array<{ name: string; args: Record<string, any> }>;
  /** Tokens consumed by this turn. */
  tokens: number;
  /** Approximate LLM cost in USD (Nous Portal-equivalent pricing). */
  llmCost: number;
  /** Whether WebLLM was used (false = mock fallback). */
  usedLlm: boolean;
};

/**
 * Handle to a booted Hermes Portable. Provides chat() and skill helpers.
 */
export class PortableHermesHandle {
  constructor(private pyodide: any, private skipLlm: boolean) {}

  /**
   * Run one chat turn through Hermes Portable. Hermes decides which
   * skills (broker_session, summarize, list_skills) to call. Skill
   * invocations are executed inside Python and surfaced back via the
   * tool_calls array.
   */
  async chat(history: ChatMessage[], opts?: { maxTokens?: number }): Promise<ChatResult> {
    const llmState = getEngineState();
    const useRealLlm = !this.skipLlm && llmState.status === 'ready';

    if (!useRealLlm) {
      // Mock fallback: parse the last user message for a simple skill trigger
      // and return a canned response. Used when WebLLM hasn't warmed up yet
      // OR when WebGPU is absent and WASM is too slow.
      return mockChat(this.pyodide, history);
    }

    // Real LLM path: stream the chat through WebLLM, parse tool calls.
    const engine = await loadWebLLM();

    // Inject the skill catalog as a system message (Hermes function-calling).
    const systemMsg: ChatMessage = {
      role: 'system',
      content: PORTABLE_SYSTEM_PROMPT,
    };
    const fullHistory = [systemMsg, ...history];

    // Pull the OpenAI-shaped tool definitions from Python.
    this.pyodide.globals.set('_max_tokens', opts?.maxTokens ?? 256);
    const toolsJson = await this.pyodide.runPythonAsync(
      'json.dumps(verdantforged.portable.tool_definitions())',
    );
    const tools = JSON.parse(String(toolsJson));

    const reply = await engine.chat.completions.create({
      messages: fullHistory as any,
      tools: tools as any,
      temperature: 0.7,
      max_tokens: opts?.maxTokens ?? 256,
    });

    const choice = reply.choices[0];
    const msg = choice.message;
    const usage = reply.usage ?? { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
    const tokens = usage.total_tokens;
    const llmCost = tokens * 0.000003;  // ~Nous Hermes-4 70B rate

    const toolCalls: ChatResult['toolCalls'] = [];
    if (msg.tool_calls && msg.tool_calls.length > 0) {
      for (const tc of msg.tool_calls) {
        try {
          const args = JSON.parse(tc.function.arguments);
          toolCalls.push({ name: tc.function.name, args });
          // Execute the skill in Python and capture the result.
          const skillResult = await this.executeSkill(tc.function.name, args);
          // Fire a DOM event so the dashboard can react (e.g. open broker session).
          window.dispatchEvent(new CustomEvent('vf:skill-called', {
            detail: { name: tc.function.name, args, result: skillResult },
          }));
        } catch (e) {
          console.error('[portable-hermes] tool call failed:', tc.function.name, e);
        }
      }
    }

    return {
      content: msg.content ?? '',
      toolCalls,
      tokens,
      llmCost,
      usedLlm: true,
    };
  }

  /**
   * Execute a named skill in Python and return its result as JSON.
   */
  private async executeSkill(name: string, args: Record<string, any>): Promise<any> {
    // Emit intent to JS dashboard first (broker_session etc).
    if (name === 'broker_session') {
      window.dispatchEvent(new CustomEvent('vf:broker-session', { detail: args }));
    }

    // Map skill names to Python entry points.
    const pyEntry: Record<string, string> = {
      list_skills: 'verdantforged.portable.list_skills',
      summarize: 'verdantforged.portable.summarize',
      broker_session: 'verdantforged.portable.broker_session',
    };
    const fnName = pyEntry[name];
    if (!fnName) {
      return { ok: false, error: `Unknown skill: ${name}` };
    }

    // Set each arg as a global before the call.
    Object.entries(args).forEach(([k, v]) => {
      this.pyodide.globals.set(`vf_arg_${k}`, typeof v === 'string' ? v : JSON.stringify(v));
    });
    const argsExpr = Object.keys(args)
      .map((k) => `vf_arg_${k}`)
      .join(', ');
    const resultJson = await this.pyodide.runPythonAsync(
      `import json; json.dumps(${fnName}(${argsExpr}))`,
    );
    return JSON.parse(String(resultJson));
  }

  /**
   * Summarize a query against the loaded docs. Used by the dashboard's
   * "Make skill call" button when WebLLM is not available.
   */
  async explain(query: string): Promise<{ summary: string; tokens: number; llmCost: number }> {
    this.pyodide.globals.set('vf_query', query);
    const summary = await this.pyodide.runPythonAsync(
      'verdantforged.portable.summarize(vf_query)',
    );
    const tokIn = await this.pyodide.runPythonAsync(
      'verdantforged.portable.last_input_tokens()',
    );
    const tokOut = await this.pyodide.runPythonAsync(
      'verdantforged.portable.last_output_tokens()',
    );
    const tokens = Number(tokIn) + Number(tokOut);
    return {
      summary: String(summary),
      tokens,
      llmCost: tokens * 0.000003,
    };
  }
}

const PORTABLE_SYSTEM_PROMPT = `You are Hermes Portable, a VerdantForged agent running in the user's browser via WebLLM.

You have access to the following skills:
- list_skills() — return your skill catalog
- summarize(query) — answer questions about the loaded docs
- broker_session(action, env?) — request the JS dashboard to open, extend, or close a VerdantForged broker session. Call this WHEN the user asks something that requires running a real skill inside an attested enclave (code execution, data analysis, file processing), or when the user asks for an operation that takes more than a single inference turn.

Decide autonomously when to call broker_session. Do not ask the user to click buttons. The session is opened automatically when you invoke the skill.

When the user asks a casual question about the docs, use summarize. When they ask for an action that needs real compute (running code, executing a workflow, anything beyond summarization), call broker_session first to provision the enclave, then summarize to answer.`;

async function mockChat(pyodide: any, history: ChatMessage[]): Promise<ChatResult> {
  // Mock fallback path — used when WebLLM isn't ready.
  const lastUser = [...history].reverse().find((m) => m.role === 'user')?.content ?? '';
  pyodide.globals.set('vf_query', lastUser);
  const summary = await pyodide.runPythonAsync(
    'verdantforged.portable.summarize(vf_query)',
  );
  const tokIn = await pyodide.runPythonAsync(
    'verdantforged.portable.last_input_tokens()',
  );
  const tokOut = await pyodide.runPythonAsync(
    'verdantforged.portable.last_output_tokens()',
  );
  const tokens = Number(tokIn) + Number(tokOut);
  return {
    content: String(summary),
    toolCalls: [],
    tokens,
    llmCost: tokens * 0.000003,
    usedLlm: false,
  };
}

/**
 * Inline Python — registers `verdantforged.portable` with Pyodide.
 *
 * Skills:
 *   - list_skills()         → catalog of {name, description, params}
 *   - summarize(query)      → fuzzy match against AGENT.md sections
 *   - broker_session(action, env?) → returns the request envelope that
 *                                     the JS dashboard will execute.
 *                                     The actual session is opened by the
 *                                     dashboard via the vf:broker-session
 *                                     DOM event.
 */
const BOOTSTRAP_PY = `
import re, sys, types, json, textwrap

class _Portable:
    """verdantforged.portable — Hermes Portable skill set for the in-browser demo.

    Real catalog (7 skills on the server side): lease_session, find_skills,
    broker_call, verify_attestation, pay_with_stripe, dashboard,
    verdantforged_explainer. We ship 3 here: list_skills, summarize,
    broker_session.
    """
    def __init__(self):
        self.sections = []
        self.cursor = 0
        self._last_in = 0
        self._last_out = 0

    def load(self, agent_md: str) -> None:
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
        self.sections = [(h, b) for h, b in out if b]
        self.cursor = 0

    def list_skills(self) -> dict:
        return {
            'ok': True,
            'skills': [
                {
                    'name': 'list_skills',
                    'description': 'Return the skill catalog.',
                    'parameters': {'type': 'object', 'properties': {}},
                },
                {
                    'name': 'summarize',
                    'description': 'Answer questions about the loaded docs by section match.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'query': {'type': 'string', 'description': 'Question or topic.'},
                        },
                        'required': ['query'],
                    },
                },
                {
                    'name': 'broker_session',
                    'description': 'Open, extend, or close a VerdantForged broker session. The session provisions a real enclave for running code or workflows that exceed single-inference turn capability.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'action': {'type': 'string', 'enum': ['open', 'extend', 'close']},
                            'env': {'type': 'string', 'description': 'Environment image (e.g. python-3.11-base). Optional.'},
                        },
                        'required': ['action'],
                    },
                },
            ],
        }

    def tool_definitions(self) -> list:
        """Return the OpenAI-shaped tool definitions for the LLM."""
        skills = self.list_skills()['skills']
        return [
            {
                'type': 'function',
                'function': {
                    'name': s['name'],
                    'description': s['description'],
                    'parameters': s['parameters'],
                },
            }
            for s in skills
        ]

    def summarize(self, query: str) -> str:
        """Fuzzy match query against AGENT.md sections."""
        if not query or not query.strip() or not self.sections:
            return self._summarize_next()
        words = [re.sub(r'[^a-z0-9]', '', w) for w in query.lower().split()]
        words = [w for w in words if len(w) >= 3]
        if not words:
            return self._summarize_next()
        phrases = [words[i] + ' ' + words[i+1] for i in range(len(words) - 1)]

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
                score += min(body_hits, 3)
            for ph in phrases:
                if ph in body_text:
                    score += 3
            if score > best_score:
                best = (h, body)
                best_score = score

        if best is None:
            return self._summarize_next()
        h, body = best
        joined = ' '.join(body)
        sentences = joined.split('. ')
        summary = '. '.join(sentences[:2]).strip()
        if not summary.endswith('.'):
            summary += '.'
        if len(summary) > 320:
            summary = summary[:317] + '…'
        try:
            self.cursor = self.sections.index(best) + 1
        except ValueError:
            pass
        self._last_in = (len(query) + len(joined)) // 4
        self._last_out = len(summary) // 4
        return f"§ {h} — {summary}"

    def _summarize_next(self) -> str:
        if not self.sections:
            return '(empty) — No sections loaded.'
        h, body = self.sections[self.cursor % len(self.sections)]
        self.cursor += 1
        joined = ' '.join(body)
        first = joined.split('. ')
        summary = first[0]
        if len(first) > 1 and len(summary) < 160:
            summary += '. ' + first[1].split('. ')[0]
        if len(summary) > 240:
            summary = summary[:237] + '…'
        self._last_in = len(joined) // 4
        self._last_out = len(summary) // 4
        return f"§ {h} — {summary}"

    def broker_session(self, action: str, env: str = 'python-3.11-base') -> dict:
        """Request the JS dashboard to open/extend/close a broker session.

        This skill does NOT open the session itself — it signals intent
        via a DOM event ('vf:broker-session') that the dashboard listens
        for. The dashboard handles the actual session lifecycle (cost,
        lease, teardown) so the cost accounting stays in JS where the
        budget envelope lives.
        """
        if action not in ('open', 'extend', 'close'):
            return {'ok': False, 'error': f'Unknown action: {action}'}
        return {
            'ok': True,
            'action': action,
            'env': env,
            'note': 'request dispatched to dashboard',
        }

    def last_input_tokens(self) -> int:
        return self._last_in

    def last_output_tokens(self) -> int:
        return self._last_out

verdantforged = types.ModuleType('verdantforged')
portable = _Portable()
verdantforged.portable = portable
sys.modules['verdantforged'] = verdantforged
sys.modules['verdantforged.portable'] = portable
`;