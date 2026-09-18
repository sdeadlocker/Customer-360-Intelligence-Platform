import { getEnvelope, postEnvelope } from '../../api/client';
import type { Envelope, Page } from '../../api/types';
import type { Maskable } from '../dashboard/widgets/format';

/**
 * The revenue-intelligence API (Phase 22 — revenue plays).
 *
 * Four plays, each of which turns something the deterministic layer already computes into a
 * *priced* opportunity rather than an observation:
 *
 *  - **Fee recovery** — charges the bank already earned but never billed (waived-and-never-restored
 *    fees, legacy pricing, unbilled services, missing relationship bundles).
 *  - **Money in motion** — a large inflow inside the short window in which it is still movable.
 *  - **Wallet share / held-away** — external holdings inferred from recurring transaction
 *    footprints, sized as a capture target.
 *  - **Economic profit / opportunity pipeline** — risk-adjusted profit per customer, and the
 *    aggregate priced pipeline with a realization rate across the other three plays.
 *
 * Types are hand-written here rather than pulled from the generated OpenAPI schema for the same
 * reason `signalsApi.ts` does it: these payloads pass through the field-masking serializer, so every
 * monetary field arrives as integer cents *or* a pre-computed band string depending on the caller's
 * role. The UI must render either and never do arithmetic on it — hence {@link Maskable} throughout
 * and {@link formatCents} at the leaf. When the backend lands, re-export `openapi.json`, run
 * `npm run codegen`, and narrow these interfaces against the generated schema.
 */

/** A resolvable pointer back to the source record a figure came from (same shape as a fact citation). */
export interface RevenueCitation {
  readonly entity_type: string;
  readonly entity_id: string;
  readonly field: string;
  readonly as_of: string;
}

export interface RevenueEvidence {
  readonly summary: string;
  readonly details?: Readonly<Record<string, string>>;
  readonly citations?: readonly RevenueCitation[];
}

// ---------------------------------------------------------------- play 8: opportunity pipeline

export type RevenuePlay =
  'FEE_RECOVERY' | 'MONEY_IN_MOTION' | 'HELD_AWAY_CAPTURE' | 'DEPOSIT_RETENTION';

export interface PipelinePlayLine {
  readonly play: RevenuePlay;
  readonly identified_cents: Maskable;
  readonly capturable_cents: Maskable;
  readonly realized_cents: Maskable;
  readonly opportunity_count: number;
}

/** The bank-wide (entitlement-scoped) priced pipeline — the executive view. */
export interface OpportunityPipeline {
  readonly identified_cents: Maskable;
  readonly capturable_cents: Maskable;
  readonly realized_cents: Maskable;
  /** Realized ÷ capturable in basis points. Null when the denominator is zero or masked. */
  readonly realization_bps: number | null;
  readonly plays: readonly PipelinePlayLine[];
  /** Money-in-motion events whose action window closes today or tomorrow. */
  readonly urgent_count: number;
  readonly book_size: number;
  readonly as_of: string;
  readonly source_system: string;
}

// ---------------------------------------------------------------- play 5: money in motion

export type InflowSource =
  | 'PROPERTY_SALE'
  | 'BONUS'
  | 'INHERITANCE'
  | 'BUSINESS_EXIT'
  | 'INSURANCE_SETTLEMENT'
  | 'UNCLASSIFIED';

export interface MoneyInMotionEvent {
  readonly event_id: number;
  readonly customer_id: string;
  readonly customer_label?: Maskable;
  readonly amount_cents: Maskable;
  readonly inferred_source: InflowSource;
  /** 0–1 confidence in the source classification. */
  readonly confidence: number;
  readonly detected_at: string;
  /** Days left in the window during which the funds are typically still movable. */
  readonly days_remaining: number;
  readonly recommended_action: string;
  readonly evidence: RevenueEvidence;
  readonly status: 'NEW' | 'ACTIONED' | 'DISMISSED';
}

// ---------------------------------------------------------------- play 4: wallet share / held-away

export type ExternalHoldingType =
  'BROKERAGE' | 'MORTGAGE' | 'CREDIT_CARD' | 'AUTO_LOAN' | 'PAYROLL' | 'INSURANCE';

export interface ExternalHolding {
  readonly holding_id: number;
  readonly holding_type: ExternalHoldingType;
  /** A non-identifying counterparty label (institution class, never an account identifier). */
  readonly counterparty: string;
  readonly estimated_value_cents: Maskable;
  readonly monthly_flow_cents: Maskable;
  readonly confidence: number;
  /** The observed transaction pattern the estimate rests on — the inference basis, shown to the user. */
  readonly basis: string;
  readonly citations?: readonly RevenueCitation[];
}

export interface WalletShare {
  readonly internal_value_cents: Maskable;
  readonly held_away_cents: Maskable;
  readonly capturable_cents: Maskable;
  readonly annual_revenue_if_captured_cents: Maskable;
  /** Internal ÷ (internal + held-away) in basis points. Null when either side is masked. */
  readonly wallet_share_bps: number | null;
  readonly is_primary_bank: boolean | null;
  readonly holdings: readonly ExternalHolding[];
  readonly as_of_date: string;
  readonly source_system: string;
}

// ---------------------------------------------------------------- play 6: fee recovery

/**
 * The billing gaps the scan reports.
 *
 * Mirrors `LeakType` in `backend/src/c360/services/fee_recovery.py`. Note what is absent: a "missing
 * relationship bundle" was in an earlier sketch of this play and was dropped, because its value is
 * customer *retention* rather than recoverable fee income — mixing the two would make the card's
 * "recoverable per year" headline mean two different things at once.
 */
export type LeakType = 'FEE_WAIVED' | 'LEGACY_PRICING' | 'UNBILLED_SERVICE' | 'MISSED_MINIMUM';

export interface FeeFinding {
  readonly finding_id: number;
  readonly leak_type: LeakType;
  /** A pre-masked account label, e.g. `Checking ****4821`. Never a full account number. */
  readonly account_label: string;
  readonly monthly_cents: Maskable;
  readonly annualized_cents: Maskable;
  /** The corpus rule the amount is measured against, for citation. */
  readonly rule_basis: string;
  /** The corpus document id behind `rule_basis`, so the citation is resolvable. */
  readonly doc_id: string;
  /** How the gap was established, in one sentence, including the basis used for a balance test. */
  readonly evidence: string;
  /** Billing cycles the gap spans. One for a per-event finding. */
  readonly cycles: number;
  readonly status: 'OPEN' | 'ACTIONED' | 'DISMISSED';
}

export interface FeeRecovery {
  readonly monthly_recoverable_cents: Maskable;
  readonly annualized_recoverable_cents: Maskable;
  readonly findings: readonly FeeFinding[];
  readonly as_of_date: string;
  readonly source_system: string;
}

// ---------------------------------------------------------------- play 8: economic profit

export interface ProfitComponent {
  readonly key: string;
  readonly label: string;
  readonly amount_cents: Maskable;
  /** CREDIT adds to profit, DEBIT subtracts. Never inferred from the sign of the number. */
  readonly direction: 'CREDIT' | 'DEBIT';
}

export interface EconomicProfit {
  readonly economic_profit_cents: Maskable;
  /** A labelled band, e.g. `VALUE_DESTROYING` / `MARGINAL` / `SOLID` / `TOP_DECILE`. */
  readonly profit_band: string;
  /** The balance-based tier the dashboard shows today (`customer_value`). */
  readonly balance_tier: string;
  /** The tier the customer sits in once profit is measured properly. */
  readonly profit_tier: string;
  /** True when the two tiers disagree — the insight that reprioritizes an RM's day. */
  readonly tier_mismatch: boolean;
  readonly segment_median_cents: Maskable;
  readonly components: readonly ProfitComponent[];
  readonly as_of_date: string;
  readonly source_system: string;
}

// ---------------------------------------------------------------- calls

/** The caller's entitlement-scoped priced pipeline across all four plays. */
export async function fetchPipeline(signal?: AbortSignal): Promise<OpportunityPipeline> {
  const envelope: Envelope<OpportunityPipeline> = await getEnvelope<OpportunityPipeline>(
    '/revenue/pipeline',
    signal,
  );
  return envelope.data;
}

/** Cross-book money-in-motion events, most urgent first. */
export async function fetchMoneyInMotion(
  { limit = 25 }: { limit?: number } = {},
  signal?: AbortSignal,
): Promise<readonly MoneyInMotionEvent[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  const envelope: Envelope<Page<MoneyInMotionEvent>> = await getEnvelope<Page<MoneyInMotionEvent>>(
    `/revenue/money-in-motion?${query.toString()}`,
    signal,
  );
  return envelope.data.items ?? [];
}

/** Mark a money-in-motion event as actioned (outreach logged) — the realization half of the loop. */
export async function acknowledgeInflow(eventId: number, signal?: AbortSignal): Promise<void> {
  await postEnvelope<{ event_id: number; status: string }>(
    `/revenue/money-in-motion/${encodeURIComponent(String(eventId))}/ack`,
    signal,
  );
}

/** Dismiss a money-in-motion event for the calling user. */
export async function dismissInflow(eventId: number, signal?: AbortSignal): Promise<void> {
  await postEnvelope<{ event_id: number; status: string }>(
    `/revenue/money-in-motion/${encodeURIComponent(String(eventId))}/dismiss`,
    signal,
  );
}

function customerPath(customerId: string, leaf: string): string {
  return `/customers/${encodeURIComponent(customerId)}/revenue/${leaf}`;
}

export async function fetchWalletShare(
  customerId: string,
  signal?: AbortSignal,
): Promise<WalletShare> {
  const envelope: Envelope<WalletShare> = await getEnvelope<WalletShare>(
    customerPath(customerId, 'wallet-share'),
    signal,
  );
  return envelope.data;
}

export async function fetchFeeRecovery(
  customerId: string,
  signal?: AbortSignal,
): Promise<FeeRecovery> {
  const envelope: Envelope<FeeRecovery> = await getEnvelope<FeeRecovery>(
    customerPath(customerId, 'fee-recovery'),
    signal,
  );
  return envelope.data;
}

export async function fetchEconomicProfit(
  customerId: string,
  signal?: AbortSignal,
): Promise<EconomicProfit> {
  const envelope: Envelope<EconomicProfit> = await getEnvelope<EconomicProfit>(
    customerPath(customerId, 'economic-profit'),
    signal,
  );
  return envelope.data;
}

export async function fetchCustomerInflows(
  customerId: string,
  signal?: AbortSignal,
): Promise<readonly MoneyInMotionEvent[]> {
  const envelope: Envelope<Page<MoneyInMotionEvent>> = await getEnvelope<Page<MoneyInMotionEvent>>(
    customerPath(customerId, 'money-in-motion'),
    signal,
  );
  return envelope.data.items ?? [];
}
