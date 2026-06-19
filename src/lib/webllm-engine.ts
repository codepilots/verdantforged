/**
 * webllm-engine.ts — singleton WebLLM loader for in-browser LLM inference.
 *
 * Hermes Portable uses WebLLM (MLC-AI) with Hermes-3-Llama-3.1-8B as the
 * runtime model. The engine is loaded lazily on the first user activity
 * (mouse/key/scroll/touch) so the ~4.5GB model download doesn't block the
 * landing page render.
 *
 * We need tool/function-calling support because the agent must invoke the
 * broker_session skill (open/extend/close) on user prompts. Phi-3.5-mini
 * does NOT support ChatCompletionRequest.tools in WebLLM 0.2.78 — only
 * the Hermes-2-Pro and Hermes-3 families do. Hermes-3-Llama-3.1-8B-q4f16_1
 * is the smallest tool-capable model in the catalog (~4.5GB weights),
 * and is the auto-selected default. If the preferred model fails to load
 * we automatically retry with the first entry in
 * `webllm.functionCallingModelIds`, so the engine is self-healing across
 * WebLLM version bumps.
 *
 * After load, every call returns the same engine instance. The model is
 * cached in the browser's Cache Storage so subsequent visits skip the
 * network download.
 *
 * Failures are non-fatal: callers check `state()` and can fall back to a
 * mock LLM (the existing Pyodide explainer) if WebLLM is unavailable
 * (no WebGPU, no WASM, model download failed).
 */

const WEBLLM_VERSION = '0.2.78';  // pinned to the verified spike version
const WEBLLM_CDN = `https://esm.run/@mlc-ai/web-llm@${WEBLLM_VERSION}`;

// Hermes-3-Llama-3.1-8B, q4f16_1 quantisation, ~4.5GB weights.
// Tool-capable: supports ChatCompletionRequest.tools (Phi-3.5-mini does
// not — see WebLLM 0.2.78's functionCallingModelIds).
export const PORTABLE_MODEL_ID = 'Hermes-3-Llama-3.1-8B-q4f16_1-MLC';

declare global {
  interface Window {
    __vfWebLLM?: Promise<any>;           // shared MLCEngine load promise
    __vfWebLLMReady?: boolean;
    __vfWebLLMProgress?: number;         // 0..1, last reported progress
  }
}

export type EngineState = {
  status: 'idle' | 'loading' | 'ready' | 'fallback' | 'error';
  progress: number;                      // 0..1
  model: string;
  backend: 'webgpu' | 'wasm' | 'unknown';
  error?: string;
};

let _state: EngineState = {
  status: 'idle',
  progress: 0,
  model: PORTABLE_MODEL_ID,
  backend: 'unknown',
};

const _listeners = new Set<(s: EngineState) => void>();

export function onEngineState(fn: (s: EngineState) => void): () => void {
  _listeners.add(fn);
  fn(_state);
  return () => _listeners.delete(fn);
}

function setState(patch: Partial<EngineState>) {
  _state = { ..._state, ...patch };
  _listeners.forEach((fn) => fn(_state));
}

export function getEngineState(): EngineState {
  return _state;
}

/**
 * Lazily load WebLLM and a tool-capable chat model. Returns a
 * callable MLCEngine. Idempotent: subsequent calls return the same promise.
 *
 * The function emits progress events via onEngineState() so the UI can
 * show "warming up" indicators. If WebGPU is absent, WebLLM falls back
 * to WASM (slower but functional). If both fail (e.g. very old browser),
 * the promise rejects with a descriptive error and callers should switch
 * to the Pyodide explainer fallback.
 */
export async function loadWebLLM(opts?: {
  /** Override the model id. Default: Hermes-3-Llama-3.1-8B-q4f16_1-MLC. */
  model?: string;
  /** Optional progress callback (0..1). */
  onProgress?: (p: number) => void;
}): Promise<any> {
  const modelId = opts?.model ?? PORTABLE_MODEL_ID;

  if (window.__vfWebLLM) {
    await window.__vfWebLLM;
    return window.__vfWebLLM;
  }

  window.__vfWebLLM = (async () => {
    setState({ status: 'loading', progress: 0 });

    // Load WebLLM as an ES module via dynamic import(). esm.run's
    // bundle exposes named exports (CreateMLCEngine, etc.) — it does
    // NOT auto-attach to window.webllm like a UMD bundle would.
    // Dynamic import() is the supported way to consume it.
    //
    // Module-level caching via window.__vfWebllmImport means we only
    // hit the network once per page; subsequent calls await the same
    // promise.
    if (!(window as any).__vfWebllmImport) {
      (window as any).__vfWebllmImport = import(/* @vite-ignore */ WEBLLM_CDN);
    }
    const webllm = await (window as any).__vfWebllmImport;
    if (!webllm?.CreateMLCEngine) {
      throw new Error(
        'WebLLM loaded but CreateMLCEngine export missing. ' +
        'Check that esm.run is reachable.',
      );
    }

    // Detect backend: WebGPU if available, else WASM fallback.
    const backend: 'webgpu' | 'wasm' = (navigator as any).gpu
      ? 'webgpu'
      : 'wasm';
    setState({ backend });

    // If WebGPU is unavailable, surface that as a friendly status
    // BEFORE CreateMLCEngine tries (and fails) to initialize the GPU.
    // The user can still use the chat — Hermes Portable falls back to
    // the Pyodide mock LLM.
    if (backend === 'wasm') {
      setState({
        status: 'fallback',
        error: 'No WebGPU — using Python mock for chat (WebLLM needs WebGPU)',
      });
    }

    const initProgressCallback = (report: { progress: number; text?: string }) => {
      const p = Math.max(0, Math.min(1, report.progress ?? 0));
      setState({ progress: p });
      opts?.onProgress?.(p);
    };

    // Build the candidate model list. We always try the preferred
    // model first; if it errors (e.g. weight download fails, model
    // renamed in a newer WebLLM catalog) we walk through the rest of
    // the tool-capable models in order. This makes the engine
    // self-healing across WebLLM version bumps — the next agent
    // won't have to repeat today's "swap Phi for Hermes-3" debug.
    const candidates: string[] = [modelId];
    const toolModels: string[] | undefined = webllm.functionCallingModelIds;
    if (Array.isArray(toolModels)) {
      for (const m of toolModels) {
        if (!candidates.includes(m)) candidates.push(m);
      }
    }
    // Track which candidate we landed on so the UI can show it.
    let lastError: unknown = null;
    for (let i = 0; i < candidates.length; i++) {
      const candidate = candidates[i];
      try {
        const engine = await webllm.CreateMLCEngine(candidate, {
          initProgressCallback,
          // Use the cached model list shipped with WebLLM.
          appConfig: undefined,
        });
        window.__vfWebLLMReady = true;
        setState({ status: 'ready', progress: 1, model: candidate });
        if (candidate !== modelId) {
          // Surface the swap so logs and any observers can see we
          // degraded from the preferred model.
          console.warn(
            `[webllm-engine] preferred model ${modelId} unavailable; ` +
            `using fallback ${candidate}.`,
          );
        }
        return engine;
      } catch (err) {
        lastError = err;
        if (i < candidates.length - 1) {
          console.warn(
            `[webllm-engine] model ${candidate} failed to load, ` +
            `trying next candidate.`,
            err,
          );
          continue;
        }
        // Exhausted candidates — rethrow the last error.
        throw err;
      }
    }
    // Unreachable: the loop either returns or throws.
    throw lastError ?? new Error('No candidate models available');
  })().catch((err) => {
    const msg = err instanceof Error ? err.message : String(err);
    setState({ status: 'error', error: msg });
    // Reset so a retry can re-attempt the load.
    window.__vfWebLLM = undefined;
    (window as any).__vfWebllmImport = undefined;
    throw err;
  });

  return window.__vfWebLLM;
}

/**
 * Warm-start the engine on the first user activity. Safe to call multiple
 * times — only the first call kicks off the load. Designed to be bound
 * to mousemove/keydown/scroll/touchstart on the landing page.
 */
let _warmedUp = false;
export function warmUpOnActivity(): void {
  if (_warmedUp) return;
  _warmedUp = true;

  const kickoff = () => {
    // Activity fired — start the ~4.5GB download in the background.
    loadWebLLM().catch(() => {
      // Failure is non-fatal; UI will read state and show fallback.
      _warmedUp = false;  // allow retry on next activity
    });
  };

  // Use rIC if available so we don't block the next paint.
  if ('requestIdleCallback' in window) {
    (window as any).requestIdleCallback(kickoff, { timeout: 1500 });
  } else {
    setTimeout(kickoff, 250);
  }
}

/**
 * Reset the singleton (used by tests and by the manual "retry" button).
 */
export function resetEngine(): void {
  window.__vfWebLLM = undefined;
  window.__vfWebLLMReady = false;
  _warmedUp = false;
  setState({ status: 'idle', progress: 0, error: undefined });
}