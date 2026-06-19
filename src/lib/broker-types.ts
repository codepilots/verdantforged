/**
 * broker-types.ts — TypeScript types for the broker RPC contract.
 *
 * The mock broker (broker-mock.ts) implements these in the browser.
 * The real broker (Rust, deployed tomorrow) implements these over HTTPS.
 * The page UI speaks only to this contract — never to either impl
 * directly.
 *
 * Schema reference: tee-broker-docs/site/BROKER_RPC.md
 */

import type { SkillCatalogEntry } from './skill-catalog';

// ─── Request envelopes ─────────────────────────────────────────

export interface IntentContext {
  client_pubkey: string;
  session_id: string | null;
  max_plan_steps: number;
  max_budget_usd: number;
  timestamp: number;
  nonce: string;
}

export interface IntentRequest {
  intent: string;
  context: IntentContext;
  signature: string;
}

export interface ApprovalRequest {
  session_id: string;
  plan_hash: string;
  user_approval: true;
  stripe_payment_intent_id: string;
  user_pubkey: string;
  timestamp: number;
  signature: string;
}

export interface RejectionRequest {
  session_id: string;
  plan_hash: string;
  reason: string;
  timestamp: number;
  signature: string;
}

export interface ModificationRequest {
  session_id: string;
  parent_plan_hash: string;
  modification_reason: string;
  original_intent: string;
  timestamp: number;
}

// ─── Response envelopes ────────────────────────────────────────

export interface PlanStep {
  step: number;
  skill: string;
  args: Record<string, unknown>;
  cost_estimate_usd: number;
  cost_estimate_predicted_usd: {
    low: number;
    high: number;
    n: number;
  };
  why: string;
}

export interface EnclaveAttestation {
  type: 'sev-snp';
  measurement: string;
  openshell_policy: string;
  signed_by: string;
}

export interface PlanResponse {
  session_id: string;
  planner_pubkey: string;
  plan: PlanStep[];
  total_cost_estimate_usd: number;
  enclave_attestation: EnclaveAttestation;
  plan_hash: string;
  parent_plan_hash: string | null;
  timestamp: number;
  signature: string;
}

export interface StepReceipt {
  step: number;
  skill: string;
  started_at: number;
  completed_at: number;
  duration_ms: number;
  fuel_used: number;
  fuel_budget: number;
  result: {
    summary: string;
    details_url?: string;
  };
  stripe_transfer_id: string;
  stripe_destination_acct: string;
  stripe_amount_usd: number;
  stripe_application_fee_usd: number;
  executor_signature: string;
}

export interface ExecutionTotals {
  estimated_cost_usd: number;
  actual_cost_usd: number;
  refund_usd: number;
  broker_retained_usd: number;
  skill_provider_paid_usd: number;
  stripe_application_fee_usd: number;
}

export interface ExecutionTrace {
  session_id: string;
  plan_hash: string;
  status: 'completed' | 'failed' | 'partial';
  trace: StepReceipt[];
  totals: ExecutionTotals;
  stripe_refund_id: string | null;
  completed_at: number;
}

export interface Ack {
  ok: boolean;
  session_id: string;
  plan_hash: string;
  stripe_void_intent: boolean;
  timestamp: number;
}

// ─── Events (for live panel updates) ───────────────────────────

export type BrokerEvent =
  | { type: 'session:opened'; session_id: string; enclave_measurement: string }
  | { type: 'planner:reasoning'; step: number; content: string }
  | { type: 'enclave:attested'; session_id: string; measurement: string; openshell_policy: string }
  | { type: 'step:started'; step: number; skill: string; fuel_budget: number }
  | { type: 'fuel:tick'; step: number; fuel_used: number; fuel_budget: number }
  | { type: 'transfer:created'; step: number; transfer_id: string; destination_acct: string; amount_usd: number; application_fee_usd: number; destination_name: string }
  | { type: 'step:completed'; step: number; result_summary: string; executor_signature: string }
  | { type: 'session:closed'; session_id: string; totals: ExecutionTotals; refund_id: string | null };

// ─── Broker interface (mock + real both implement) ─────────────

export interface BrokerClient {
  intent(req: IntentRequest): Promise<PlanResponse>;
  approve(req: ApprovalRequest): Promise<ExecutionTrace>;
  reject(req: RejectionRequest): Promise<Ack>;
  modify(req: ModificationRequest): Promise<PlanResponse>;
  subscribe(fn: (e: BrokerEvent) => void): () => void;
  // Internal: used by the mock to compute the cost predictions.
  _catalog: ReadonlyArray<SkillCatalogEntry>;
}
