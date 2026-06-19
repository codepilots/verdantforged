/**
 * session-dashboard.ts — shared client-side logic for the SessionDashboard
 * tile. Used by both:
 *   - SessionDashboard.astro  (full-size, on the landing page)
 *   - BrowserAgentSplash.astro (compact, inside the Pyodide modal)
 *
 * The tile has two states:
 *   - idle:    "No active session" + a button to provision one
 *   - active:  countdown clock, spent amount, progress meter, three affordances
 *
 * Tick interval (1s) accrues broker cost at $0.0133/min. Each skill call adds
 * $0.0050. Refund on close = max_budget - totalUsd.
 *
 * The DOM contract:
 *   <article id="<rootId>" data-session-active="false|true">
 *     <div id="<rootId>-idle">      ... idle UI, hidden when active ...
 *     <div id="<rootId>-active" class="hidden">
 *       [data-role="clock"]        text element for mmss
 *       [data-role="elapsed"]      text element for elapsed mmss (full only)
 *       [data-role="total"]        text element for $0.0000
 *       [data-role="broker-cost"]  text element (full only)
 *       [data-role="skill-cost"]   text element (full only)
 *       [data-role="llm-cost"]     text element (full only, optional)
 *       [data-role="meter"]        div whose style.width = "X%"
 *       [data-role="budget-fill"]  alias used on landing page
 *       [data-role="log"]          call log container (optional)
 *     </div>
 *   </article>
 *
 *   Buttons anywhere on the page (the helper scopes them to rootId):
 *     [data-action="<rootId>-open-session"]
 *     [data-action="<rootId>-skill-call"]
 *     [data-action="<rootId>-extend"]
 *     [data-action="<rootId>-close"]
 *
 * Multiple tiles can coexist on one page; each gets a unique rootId.
 */

export type DashboardConfig = {
  rootId: string;
  maxBudgetUsd: number;
  costPerMin: number;       // broker compute cost per minute
  skillCostUsd: number;     // per-call skill cost
  leaseSeconds: number;     // initial lease (e.g. 300 = 5 min)
  extendSeconds: number;    // minutes added per extend click (e.g. 120)
  sessionShort: string;     // displayed session id (truncated, e.g. "sess_…")
  environment: string;      // e.g. "python-3.11-base"
};

export type DashboardHandle = {
  open: () => void;
  makeSkillCall: () => void;
  extend: () => void;
  close: (reason?: string) => void;
  isActive: () => boolean;
  getSnapshot: () => {
    active: boolean;
    remaining: number;
    elapsed: number;
    totalUsd: number;
    callCount: number;
  };
};

function fmtMoney(n: number): string {
  return `$${n.toFixed(4)}`;
}
function fmtClock(secs: number): string {
  const m = Math.max(0, Math.floor(secs / 60));
  const s = Math.max(0, secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function createDashboard(cfg: DashboardConfig): DashboardHandle {
  const root = document.getElementById(cfg.rootId);
  if (!root) {
    throw new Error(`createDashboard: root #${cfg.rootId} not found`);
  }
  const rootEl: HTMLElement = root;
  const idle    = document.getElementById(`${cfg.rootId}-idle`);
  const active  = document.getElementById(`${cfg.rootId}-active`);
  const clock   = rootEl.querySelector<HTMLElement>('[data-role="clock"]');
  const elapsed = rootEl.querySelector<HTMLElement>('[data-role="elapsed"]');
  const total   = rootEl.querySelector<HTMLElement>('[data-role="total"]');
  const broker  = rootEl.querySelector<HTMLElement>('[data-role="broker-cost"]');
  const skill   = rootEl.querySelector<HTMLElement>('[data-role="skill-cost"]');
  const llm     = rootEl.querySelector<HTMLElement>('[data-role="llm-cost"]');
  const meter   = rootEl.querySelector<HTMLElement>('[data-role="meter"], [data-role="budget-fill"]');
  const log     = rootEl.querySelector<HTMLElement>('[data-role="log"]');

  // Mutable state
  let activeFlag = false;
  let closed = false;
  let remaining = cfg.leaseSeconds;
  let elapsedSec = 0;
  let totalUsd = 0;
  let callCount = 0;
  let tickId: number | null = null;
  let logFirstWiped = false;

  function render() {
    if (!idle || !active) return;
    if (activeFlag) {
      idle.classList.add('hidden');
      active.classList.remove('hidden');
      rootEl.dataset.sessionActive = 'true';
    } else {
      active.classList.add('hidden');
      idle.classList.remove('hidden');
      rootEl.dataset.sessionActive = 'false';
    }
    if (clock)   clock.textContent   = fmtClock(remaining);
    if (elapsed) elapsed.textContent = fmtClock(elapsedSec);
    if (total)   total.textContent   = fmtMoney(totalUsd);
    if (broker)  broker.textContent  = fmtMoney((elapsedSec / 60) * cfg.costPerMin);
    if (skill)   skill.textContent   = fmtMoney(callCount * cfg.skillCostUsd);
    if (llm)     llm.textContent     = fmtMoney(0); // v1: LLM cost baked into broker
    if (meter) {
      const pct = Math.min(100, Math.round((totalUsd / cfg.maxBudgetUsd) * 100));
      meter.style.width = `${pct}%`;
    }
  }

  function appendLog(html: string) {
    if (!log) return;
    // Wipe the placeholder on the first action of the first session only.
    if (!logFirstWiped) {
      log.innerHTML = '';
      logFirstWiped = true;
    }
    const entry = document.createElement('p');
    entry.className = 'text-ink';
    entry.innerHTML = html;
    log.appendChild(entry);
  }

  function tick() {
    if (!activeFlag || closed) return;
    remaining = Math.max(0, remaining - 1);
    elapsedSec += 1;
    const brokerCost = (elapsedSec / 60) * cfg.costPerMin;
    totalUsd = brokerCost + callCount * cfg.skillCostUsd;
    render();
    if (remaining === 0) {
      close('Lease expired');
    }
  }

  function open() {
    if (activeFlag) return;
    activeFlag = true;
    closed = false;
    remaining = cfg.leaseSeconds;
    elapsedSec = 0;
    totalUsd = 0;
    callCount = 0;
    render();
    tickId = window.setInterval(tick, 1000);
    appendLog(`<span class="text-verdigris-deep">▸</span> Session opened · lease ${fmtClock(cfg.leaseSeconds)} · budget ${fmtMoney(cfg.maxBudgetUsd)}`);
  }

  function makeSkillCall() {
    if (!activeFlag || closed) return;
    if (totalUsd + cfg.skillCostUsd > cfg.maxBudgetUsd) {
      appendLog('<span class="text-copper-deep">▸</span> Budget exceeded — close or extend the session');
      return;
    }
    callCount += 1;
    tick();   // recompute total + render
    appendLog(`<span class="text-verdigris-deep">▸</span> broker_call · ${cfg.environment} · +${fmtMoney(cfg.skillCostUsd)}`);
  }

  function extend() {
    if (!activeFlag || closed) return;
    remaining += cfg.extendSeconds;
    render();
    appendLog(`<span class="text-verdigris-deep">▸</span> Extended +${fmtClock(cfg.extendSeconds)} · escrow incremented pro-rata`);
  }

  function close(reason = 'User closed') {
    if (!activeFlag || closed) return;
    closed = true;
    activeFlag = false;
    if (tickId !== null) {
      window.clearInterval(tickId);
      tickId = null;
    }
    const refund = Math.max(0, cfg.maxBudgetUsd - totalUsd);
    appendLog(`<span class="text-copper-deep">▸</span> ${reason} · refund ${fmtMoney(refund)} released`);
    render();
  }

  // Wire buttons scoped to this dashboard
  const wire = (selector: string, fn: () => void) => {
    document.querySelectorAll<HTMLElement>(`[data-action="${cfg.rootId}-${selector}"]`).forEach((b) => {
      b.addEventListener('click', fn);
    });
  };
  wire('open-session', open);
  wire('skill-call',   makeSkillCall);
  wire('extend',       extend);
  wire('close',        () => close());

  // Render once on creation so the initial idle state is shown correctly
  render();

  return {
    open,
    makeSkillCall,
    extend,
    close,
    isActive: () => activeFlag && !closed,
    getSnapshot: () => ({ active: activeFlag && !closed, remaining, elapsed: elapsedSec, totalUsd, callCount }),
  };
}