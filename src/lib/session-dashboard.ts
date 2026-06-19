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
  skillCostUsd: number;     // per-call skill cost (paid to broker)
  leaseSeconds: number;     // initial lease (e.g. 300 = 5 min)
  extendSeconds: number;    // minutes added per extend click (e.g. 120)
  sessionShort: string;     // displayed session id (truncated, e.g. "sess_…")
  environment: string;      // e.g. "python-3.11-base"
  /**
   * Optional hook invoked when "Make skill call" is clicked. The dashboard
   * awaits the promise and surfaces the result in the call log. If the
   * promise rejects, the call is rolled back (no cost charged, no count
   * increment) and the error message is logged in copper.
   *
   * Return value:
   *   - summary:   a short human-readable string appended to the call log
   *   - llmCost:   dollar amount to add to the LLM cost column (Nous
   *                inference, Nous Portal per-token pricing). Defaults to 0
   *                when omitted.
   *   - tokens:    total tokens used (input + output). Optional.
   *
   * If onSkillCall is omitted, the dashboard behaves as before: just bump
   * the counter, log "broker_call · <env> · +$0.0050", no real work.
   */
  onSkillCall?: () => Promise<{
    summary: string;
    llmCost?: number;
    tokens?: number;
  }>;
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
  let llmUsdTotal = 0;       // accumulated Nous inference cost
  let tickId: number | null = null;
  let logFirstWiped = false;
  let skillCallInFlight = false;

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
    if (llm)     llm.textContent     = fmtMoney(llmUsdTotal);
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

  async function makeSkillCall() {
    if (!activeFlag || closed) return;
    if (skillCallInFlight) return;  // de-dupe rapid clicks
    if (totalUsd + cfg.skillCostUsd > cfg.maxBudgetUsd) {
      appendLog('<span class="text-copper-deep">▸</span> Budget exceeded — close or extend the session');
      return;
    }
    skillCallInFlight = true;
    const callN = callCount + 1;
    appendLog(`<span class="text-ink-soft">▸</span> broker_call #${callN} · ${cfg.environment} · <em>running…</em>`);

    if (!cfg.onSkillCall) {
      // No agent wired: v1 mock behaviour — count it, charge skill cost.
      callCount += 1;
      skillCallInFlight = false;
      tick();
      appendLog(`<span class="text-verdigris-deep">▸</span> mock_call #${callCount} · +${fmtMoney(cfg.skillCostUsd)}`);
      return;
    }

    try {
      const result = await cfg.onSkillCall();
      // Reserve budget up-front so a slow agent can't exceed it.
      callCount += 1;
      const llmCost = Math.max(0, result.llmCost ?? 0);
      llmUsdTotal += llmCost;
      const tokenSuffix = result.tokens ? ` · ${result.tokens} tok` : '';
      const llmSuffix = llmCost > 0 ? ` · llm +${fmtMoney(llmCost)}` : '';
      tick();
      appendLog(
        `<span class="text-verdigris-deep">▸</span> ` +
        `<strong>${escapeHtml(result.summary)}</strong>` +
        ` <span class="text-ink-soft">(${cfg.environment} · +${fmtMoney(cfg.skillCostUsd)}${llmSuffix}${tokenSuffix})</span>`
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      appendLog(`<span class="text-copper-deep">▸</span> skill_call failed · ${escapeHtml(msg)} · no charge`);
    } finally {
      skillCallInFlight = false;
    }
  }

  // Minimal HTML escape for log entries (summary strings come from
  // agent output; trust nothing).
  function escapeHtml(s: string): string {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
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