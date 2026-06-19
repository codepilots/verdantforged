/**
 * webllm-engine.ts — singleton WebLLM loader for in-browser LLM inference.
 *
 * Hermes Portable uses WebLLM (MLC-AI) with Phi-3.5-mini-instruct as the
 * runtime model. The engine is loaded lazily on the first user activity
 * (mouse/key/scroll/touch) so the 2GB model download doesn't block the
 * landing page render.
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

// Phi-3.5-mini-instruct, q4f16_1 quantisation, ~2.0GB VRAM. Best
// function-calling in WebLLM's catalog per browser-pyodide-agent-runtime
// skill pitfall #3.
export const PORTABLE_MODEL_ID = 'Phi-3.5-mini-instruct-q4f16_1-MLC';

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
 * Lazily load WebLLM and the Phi-3.5-mini-instruct model. Returns a
 * callable MLCEngine. Idempotent: subsequent calls return the same promise.
 *
 * The function emits progress events via onEngineState() so the UI can
 * show "warming up" indicators. If WebGPU is absent, WebLLM falls back
 * to WASM (slower but functional). If both fail (e.g. very old browser),
 * the promise rejects with a descriptive error and callers should switch
 * to the Pyodide explainer fallback.
 */
export async function loadWebLLM(opts?: {
  /** Override the model id. Default: Phi-3.5-mini-instruct-q4f16_1-MLC. */
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

    // Inject the WebLLM module from esm.run (idempotent — check first).
    if (!(window as any).webllm) {
      const existing = document.querySelector('script[data-vf-webllm]');
      if (!existing) {
        const s = document.createElement('script');
        s.type = 'module';
        s.src = WEBLLM_CDN;
        s.dataset.vfWebllm = 'true';
        document.head.appendChild(s);
        // The ESM module exports `CreateMLCEngine` on `window.webllm`.
        // esm.run attaches the named exports to a `webllm` global.
        await waitForGlobal('webllm', 15000);
      }
    }

    const webllm = (window as any).webllm;
    if (!webllm?.CreateMLCEngine) {
      throw new Error(
        'WebLLM failed to attach to window from CDN. ' +
        'Check network access to esm.run.',
      );
    }

    // Detect backend: WebGPU if available, else WASM fallback.
    const backend: 'webgpu' | 'wasm' = (navigator as any).gpu
      ? 'webgpu'
      : 'wasm';
    setState({ backend });

    const initProgressCallback = (report: { progress: number; text?: string }) => {
      const p = Math.max(0, Math.min(1, report.progress ?? 0));
      setState({ progress: p });
      opts?.onProgress?.(p);
    };

    const engine = await webllm.CreateMLCEngine(modelId, {
      initProgressCallback,
      // Use the cached model list shipped with WebLLM.
      appConfig: undefined,
    });

    window.__vfWebLLMReady = true;
    setState({ status: 'ready', progress: 1 });
    return engine;
  })().catch((err) => {
    const msg = err instanceof Error ? err.message : String(err);
    setState({ status: 'error', error: msg });
    // Reset so a retry can re-attempt the load.
    window.__vfWebLLM = undefined;
    throw err;
  });

  return window.__vfWebLLM;
}

/**
 * Wait until `window[globalName]` exists. Polls every 50ms up to
 * `timeoutMs`. Resolves with the value, rejects on timeout.
 */
function waitForGlobal(globalName: string, timeoutMs: number): Promise<any> {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = () => {
      const v = (window as any)[globalName];
      if (v) return resolve(v);
      if (Date.now() - start > timeoutMs) {
        return reject(
          new Error(`Timed out waiting for window.${globalName}`),
        );
      }
      setTimeout(check, 50);
    };
    check();
  });
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
    // Activity fired — start the 2GB download in the background.
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