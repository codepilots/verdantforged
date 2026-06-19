/**
 * broker-mock.ts — browser-side implementation of the broker RPC contract.
 *
 * This is the engine that powers the demo. It implements every envelope
 * from BROKER_RPC.md, emits live events for the three sponsor panels, and
 * produces realistic-looking Stripe test-mode IDs and SEV-SNP attestation
 * strings.
 *
 * When the real Rust broker is online, swap this file for a fetch-based
 * client (see broker.ts). The page UI doesn't change.
 *
 * Mock pacing: each step takes ~1.2–2.5s of simulated execution so
 * judges can watch the panels fill in real time.
 */

import type {
  BrokerClient,
  BrokerEvent,
  IntentRequest,
  PlanResponse,
  ApprovalRequest,
  ExecutionTrace,
  RejectionRequest,
  Ack,
  ModificationRequest,
  StepReceipt,
  ExecutionTotals,
  PlanStep,
} from './broker-types';
import { SKILL_CATALOG, BROKER_PLATFORM_ACCT, BROKER_PLATFORM_NAME, predictedRange } from './skill-catalog';
import { matchIntentToSkills, mockStepResult } from './intent-matcher';

// ─── Constants ────────────────────────────────────────────────

const SYNTHETIC_MEASUREMENT = '8f3a9c2b1d4e7f0a6b5c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a';
const SYNTHETIC_PLANNER_PUBKEY = 'ed25519:synthetic_demo_planner_pubkey_9f8e7d6c5b4a3f2e';
const SYNTHETIC_OPENSHELL = 'default-deny; allow api.stripe.com:443 only';
const APP_FEE_RATE = 0.05; // 5% application fee on each transfer
const FUEL_BUDGET_PER_STEP = 50_000_000;

// ─── Utilities ────────────────────────────────────────────────

function rand(n: number): string {
  // Stable-ish random for deterministic demo IDs
  return Math.random().toString(36).slice(2, 2 + n);
}

function nowSec(): number {
  return Math.floor(Date.now() / 1000);
}

async function sleep(ms: number): Promise<void> {
  return new Promise(res => setTimeout(res, ms));
}

function fakeSign(payload: string): string {
  // SHA-256 of payload, hex-encoded, prefixed with ed25519: for shape.
  // The real broker signs with the enclave's ephemeral Ed25519 key.
  // We use a simple hash here because we're in the browser with no crypto.
  // (SubtleCrypto is async-only; the rest of this file is sync.)
  let h = 0;
  for (let i = 0; i < payload.length; i++) {
    h = ((h << 5) - h + payload.charCodeAt(i)) | 0;
  }
  const fake = (h >>> 0).toString(16).padStart(8, '0');
  return `ed25519:synthetic_sig_${fake}${rand(20)}`;
}

async function realSign(payload: string): Promise<string> {
  // Use SubtleCrypto when available for a more believable signature.
  if (typeof crypto !== 'undefined' && crypto.subtle) {
    const data = new TextEncoder().encode(payload);
    const buf = await crypto.subtle.digest('SHA-256', data);
    const hex = Array.from(new Uint8Array(buf))
      .map(b => b.toString(16).padStart(2, '0'))
      .join('');
    return `ed25519:sha256_${hex}${rand(8)}`;
  }
  return fakeSign(payload);
}

// ─── Mock Broker ──────────────────────────────────────────────

class BrokerMock implements BrokerClient {
  public readonly _catalog = SKILL_CATALOG;

  private subscribers = new Set<(e: BrokerEvent) => void>();

  subscribe(fn: (e: BrokerEvent) => void): () => void {
    this.subscribers.add(fn);
    return () => this.subscribers.delete(fn);
  }

  private emit(e: BrokerEvent): void {
    this.subscribers.forEach(fn => {
      try { fn(e); } catch { /* swallow subscriber errors */ }
    });
  }

  // ─── intent ─────────────────────────────────────────────────

  async intent(req: IntentRequest): Promise<PlanResponse> {
    const session_id = `sess_${rand(10)}`;
    const match = matchIntentToSkills(req.intent);
    const allSkills = [match.primary, ...match.followups];

    const plan: PlanStep[] = allSkills.map((s, i) => ({
      step: i + 1,
      skill: s.name,
      args: { ...s.default_args, _matched_for: req.intent.slice(0, 80) },
      cost_estimate_usd: s.static_cost_usd,
      cost_estimate_predicted_usd: predictedRange(s.static_cost_usd, s.n_executions, nowSec() + i),
      why: `${s.why_template(req.intent)} — ${match.reason}`,
    }));

    const total_cost_estimate_usd = +plan
      .reduce((sum, p) => sum + p.cost_estimate_usd, 0)
      .toFixed(4);

    const plan_hash = await sha256Of(JSON.stringify(plan));
    const timestamp = nowSec();
    const signature = await realSign(`${plan_hash}|${session_id}|${timestamp}`);

    return {
      session_id,
      planner_pubkey: SYNTHETIC_PLANNER_PUBKEY,
      plan,
      total_cost_estimate_usd,
      enclave_attestation: {
        type: 'sev-snp',
        measurement: SYNTHETIC_MEASUREMENT,
        openshell_policy: SYNTHETIC_OPENSHELL,
        signed_by: 'AMD SEV-SNP root CA (synthetic for demo)',
      },
      plan_hash,
      parent_plan_hash: null,
      timestamp,
      signature,
    };
  }

  // ─── approve (the centerpiece — emits all events) ───────────

  async approve(req: ApprovalRequest): Promise<ExecutionTrace> {
    const { session_id, plan_hash, stripe_payment_intent_id } = req;
    const trace: StepReceipt[] = [];

    // session:opened
    this.emit({
      type: 'session:opened',
      session_id,
      enclave_measurement: SYNTHETIC_MEASUREMENT,
    });
    await sleep(120);

    // enclave:attested
    this.emit({
      type: 'enclave:attested',
      session_id,
      measurement: SYNTHETIC_MEASUREMENT,
      openshell_policy: SYNTHETIC_OPENSHELL,
    });
    await sleep(180);

    // We need the plan to compute per-step timing. Recover it from the
    // signature-less match. In real mode, the broker would have stored
    // the plan when intent() was called. Here we re-run the matcher
    // against a placeholder; the visible timing/IDs are what matter
    // for the demo. The actual plan came from the intent() call.

    // Simulate 2 steps by default (most demos use primary + summarize).
    // For a richer demo we run 2 steps.
    const stepsToRun = 2;
    const skillsPerStep = this.pickDemoSkillsForSession();

    for (let i = 0; i < stepsToRun; i++) {
      const skillEntry = skillsPerStep[i];
      const stepNum = i + 1;

      // planner:reasoning for this step
      this.emit({
        type: 'planner:reasoning',
        step: stepNum,
        content: `picked ${skillEntry.name} (${skillEntry.env}, n=${skillEntry.n_executions}, ★${skillEntry.reputation})`,
      });
      await sleep(150);

      // step:started
      this.emit({
        type: 'step:started',
        step: stepNum,
        skill: skillEntry.name,
        fuel_budget: FUEL_BUDGET_PER_STEP,
      });
      await sleep(80);

      // fuel:tick — 4 ticks so the panel shows a moving counter
      const durationMs = 800 + Math.floor(Math.random() * 1400);
      const tickCount = 4;
      const ticksDelay = Math.floor(durationMs / tickCount);
      const finalFuel = Math.floor(FUEL_BUDGET_PER_STEP * (0.05 + Math.random() * 0.20));

      for (let t = 0; t < tickCount; t++) {
        const progress = (t + 1) / tickCount;
        await sleep(ticksDelay);
        this.emit({
          type: 'fuel:tick',
          step: stepNum,
          fuel_used: Math.floor(finalFuel * progress),
          fuel_budget: FUEL_BUDGET_PER_STEP,
        });
      }

      // transfer:created
      const transfer_id = `tr_test_${rand(24)}`;
      const appFee = +(skillEntry.static_cost_usd * APP_FEE_RATE).toFixed(4);
      this.emit({
        type: 'transfer:created',
        step: stepNum,
        transfer_id,
        destination_acct: skillEntry.provider_acct,
        destination_name: skillEntry.provider_name,
        amount_usd: skillEntry.static_cost_usd,
        application_fee_usd: appFee,
      });
      await sleep(180);

      // step:completed
      const resultSummary = mockStepResult(skillEntry, skillsPerStep[0]?.name ?? '');
      const executorSig = await realSign(`${plan_hash}|step:${stepNum}|complete`);
      this.emit({
        type: 'step:completed',
        step: stepNum,
        result_summary: resultSummary,
        executor_signature: executorSig,
      });

      trace.push({
        step: stepNum,
        skill: skillEntry.name,
        started_at: nowSec(),
        completed_at: nowSec(),
        duration_ms: durationMs,
        fuel_used: finalFuel,
        fuel_budget: FUEL_BUDGET_PER_STEP,
        result: { summary: resultSummary },
        stripe_transfer_id: transfer_id,
        stripe_destination_acct: skillEntry.provider_acct,
        stripe_amount_usd: skillEntry.static_cost_usd,
        stripe_application_fee_usd: appFee,
        executor_signature: executorSig,
      });
    }

    // totals
    const totalActual = +trace.reduce((s, r) => s + r.stripe_amount_usd, 0).toFixed(4);
    const totalAppFee = +trace.reduce((s, r) => s + r.stripe_application_fee_usd, 0).toFixed(4);
    const totalEstimated = totalActual; // mock: estimate == actual
    const refundUsd = +(Math.max(0, totalEstimated - totalActual)).toFixed(4);
    const totals: ExecutionTotals = {
      estimated_cost_usd: totalEstimated,
      actual_cost_usd: totalActual,
      refund_usd: refundUsd,
      broker_retained_usd: totalAppFee,
      skill_provider_paid_usd: +(totalActual - totalAppFee).toFixed(4),
      stripe_application_fee_usd: totalAppFee,
    };
    const refund_id = refundUsd > 0 ? `re_test_${rand(24)}` : null;

    // session:closed
    await sleep(200);
    this.emit({
      type: 'session:closed',
      session_id,
      totals,
      refund_id,
    });

    return {
      session_id,
      plan_hash,
      status: 'completed',
      trace,
      totals,
      stripe_refund_id: refund_id,
      completed_at: nowSec(),
    };
  }

  // ─── reject ────────────────────────────────────────────────

  async reject(req: RejectionRequest): Promise<Ack> {
    return {
      ok: true,
      session_id: req.session_id,
      plan_hash: req.plan_hash,
      stripe_void_intent: true, // page should call paymentIntents.cancel
      timestamp: nowSec(),
    };
  }

  // ─── modify ────────────────────────────────────────────────

  async modify(req: ModificationRequest): Promise<PlanResponse> {
    // For the mock, modify just returns a similar plan with parent_plan_hash set.
    const match = matchIntentToSkills(`${req.original_intent} (modified: ${req.modification_reason})`);
    const allSkills = [match.primary];

    const plan: PlanStep[] = allSkills.map((s, i) => ({
      step: i + 1,
      skill: s.name,
      args: { ...s.default_args, _modification: req.modification_reason.slice(0, 80) },
      cost_estimate_usd: s.static_cost_usd,
      cost_estimate_predicted_usd: predictedRange(s.static_cost_usd, s.n_executions, nowSec() + i),
      why: `modified: ${req.modification_reason}`,
    }));

    const total_cost_estimate_usd = +plan.reduce((sum, p) => sum + p.cost_estimate_usd, 0).toFixed(4);
    const plan_hash = await sha256Of(JSON.stringify(plan) + req.parent_plan_hash);
    const timestamp = nowSec();
    const signature = await realSign(`${plan_hash}|${req.session_id}|${timestamp}`);

    return {
      session_id: req.session_id,
      planner_pubkey: SYNTHETIC_PLANNER_PUBKEY,
      plan,
      total_cost_estimate_usd,
      enclave_attestation: {
        type: 'sev-snp',
        measurement: SYNTHETIC_MEASUREMENT,
        openshell_policy: SYNTHETIC_OPENSHELL,
        signed_by: 'AMD SEV-SNP root CA (synthetic for demo)',
      },
      plan_hash,
      parent_plan_hash: req.parent_plan_hash,
      timestamp,
      signature,
    };
  }

  // ─── Demo helper ───────────────────────────────────────────

  /**
   * Pick which skills this session will execute.
   * For the demo we hardcode a sensible pair: code-review + summarize.
   * (The intent-matcher already returned the primary, but the mock
   * simulates the EXECUTOR side — which runs whatever the plan said.)
   */
  private pickDemoSkillsForSession() {
    const codeReview = SKILL_CATALOG.find(s => s.name === 'code-review-v3.2')!;
    const summarize = SKILL_CATALOG.find(s => s.name === 'summarize-doc')!;
    return [codeReview, summarize];
  }
}

// ─── Hash helper ──────────────────────────────────────────────

async function sha256Of(text: string): Promise<string> {
  if (typeof crypto !== 'undefined' && crypto.subtle) {
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    const hex = Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
    return `sha256:${hex}`;
  }
  // Fallback for environments without SubtleCrypto (shouldn't happen in browser)
  let h = 0;
  for (let i = 0; i < text.length; i++) h = ((h << 5) - h + text.charCodeAt(i)) | 0;
  return `sha256:${(h >>> 0).toString(16).padStart(8, '0')}`;
}

export const broker = new BrokerMock();
