/**
 * auto-launch.ts — boots Hermes Portable in the background on first
 * user activity. No button press required.
 *
 * Two-phase startup:
 *   Phase 1 (on first user activity): start WebLLM model download.
 *     Triggered by mousemove/keydown/scroll/touchstart. The 2GB
 *     download happens silently while the user reads the landing page.
 *   Phase 2 (on chat input focus): start Pyodide + Hermes Portable.
 *     Triggered by focusing any [data-role="prompt-input"] across the
 *     site. ~20MB download + Python boot. By the time the user types
 *     the first prompt, both are usually warm.
 *
 * UI side: a small "warming up" pill is shown via vf:engine-state
 * custom events. The page renders normally throughout.
 */

import {
  warmUpOnActivity,
  onEngineState,
  getEngineState,
} from './webllm-engine';
import { loadPortableHermes, type PortableHermesHandle } from './portable-hermes';

declare global {
  interface Window {
    __vfPortable?: Promise<PortableHermesHandle>;
  }
}

let _activityBound = false;
let _phase2Triggered = false;

/**
 * Bind the auto-launch listeners. Call once per page. Idempotent.
 */
export function installAutoLaunch(): void {
  if (_activityBound) return;
  _activityBound = true;

  // Phase 1 — fire on first user activity (any kind).
  const events: Array<keyof DocumentEventMap> = [
    'mousemove',
    'keydown',
    'scroll',
    'touchstart',
    'pointerdown',
  ];
  const kickOnce = () => {
    warmUpOnActivity();
    events.forEach((ev) =>
      document.removeEventListener(ev, kickOnce, { capture: true } as any),
    );
  };
  events.forEach((ev) =>
    document.addEventListener(ev, kickOnce, { passive: true, capture: true } as any),
  );

  // Phase 2 — fire when user focuses a chat input anywhere on the page.
  // Use focusin (bubbles) instead of focus (doesn't bubble).
  document.addEventListener(
    'focusin',
    (e) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      if (
        target.matches?.('[data-role="prompt-input"]') ||
        target.matches?.('input[type="text"], textarea')
      ) {
        kickPhase2();
      }
    },
    { passive: true },
  );

  // Expose engine state on a small status pill (if present).
  onEngineState((state) => {
    const pill = document.querySelector<HTMLElement>('[data-role="engine-status"]');
    if (pill) updateStatusPill(pill, state);
    window.dispatchEvent(
      new CustomEvent('vf:engine-state', { detail: state }),
    );
  });
}

/**
 * Start the Pyodide + Hermes Portable boot. Called from focusin above
 * OR from the page-level script when the chat surface becomes visible.
 */
export function kickPhase2(): void {
  if (_phase2Triggered) return;
  _phase2Triggered = true;

  window.__vfPortable ??= loadPortableHermes().catch((err) => {
    console.error('[auto-launch] Hermes Portable boot failed:', err);
    _phase2Triggered = false;  // allow retry
    throw err;
  });
}

/**
 * Await Hermes Portable, booting it now if not already started.
 * Used by the chat UI on first message send.
 */
export async function getPortableHermes(): Promise<PortableHermesHandle> {
  // Make sure both phases have fired at least once.
  warmUpOnActivity();
  kickPhase2();
  return window.__vfPortable!;
}

/**
 * Update a small status pill that shows engine state. Renders text
 * only — no styling here; the host page sets the visual.
 */
function updateStatusPill(pill: HTMLElement, state: ReturnType<typeof getEngineState>): void {
  const { status, progress, backend, error } = state;
  let label: string;
  let cls: string;

  switch (status) {
    case 'idle':
      label = 'idle · awaiting activity';
      cls = 'vf-engine-idle';
      break;
    case 'loading':
      label = `warming up · ${Math.round(progress * 100)}% · ${backend}`;
      cls = 'vf-engine-loading';
      break;
    case 'ready':
      label = `ready · ${backend}`;
      cls = 'vf-engine-ready';
      break;
    case 'fallback':
      label = 'fallback · python mock';
      cls = 'vf-engine-fallback';
      break;
    case 'error':
      label = `error · ${error ?? 'unknown'}`;
      cls = 'vf-engine-error';
      break;
  }

  pill.textContent = label;
  pill.dataset.state = status;
  pill.className = cls;
}