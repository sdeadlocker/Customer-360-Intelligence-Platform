/**
 * Widget-facing TypeScript shapes for the Phase 13 dashboard.
 *
 * The composed `/customers/{id}/360` payload is `dict[str, Any]` on the wire (the backend builds it
 * dynamically and masks each module independently), so the generated OpenAPI client types it as an
 * open object. These interfaces are the refinement the design calls for: each widget reads its own
 * slice as one of these shapes rather than trusting the loose type. They mirror the backend's
 * serialized field names exactly (see `backend/src/c360/api/routes/customers.py` and
 * `domain/models.py`).
 *
 * Every field that can be masked is typed with {@link Maskable} — `number | string | null |
 * undefined` — because the server returns the real value, a band/partial string, or omits the key
 * entirely depending on the caller's role. A widget must handle all three; the {@link
 * ./format} helpers do exactly that.
 */

import type { Maskable } from './format';

export interface ProfileData {
  readonly customer_id: string;
  readonly customer_name?: Maskable;
  readonly customer_type?: string;
  readonly customer_segment?: string;
  readonly customer_since?: string;
  readonly date_of_birth?: Maskable;
  readonly citizenship?: string | null;
  readonly occupation?: string | null;
  readonly employer_id?: string | null;
  readonly employment_status?: string | null;
  readonly marital_status?: string | null;
  readonly customer_value?: Maskable;
  readonly customer_value_score?: number | null;
  readonly preferred_language?: string;
  readonly preferred_channel?: string | null;
  readonly household_id?: string | null;
  readonly as_of_date?: string;
  readonly source_system?: string;
}

export interface ContactData {
  readonly customer_id: string;
  readonly email?: Maskable;
  readonly phone_number?: Maskable;
  readonly mobile_number?: Maskable;
  readonly address_line1?: Maskable;
  readonly address_line2?: Maskable;
  readonly city?: string | null;
  readonly state?: string | null;
  readonly country?: string | null;
  readonly postal_code?: Maskable;
  readonly as_of_date?: string;
  readonly source_system?: string;
}

export interface FinancialProfileData {
  readonly customer_id: string;
  readonly total_deposits_cents?: Maskable;
  readonly total_loans_cents?: Maskable;
  readonly total_investments_cents?: Maskable;
  readonly total_assets_cents?: Maskable;
  readonly total_liabilities_cents?: Maskable;
  readonly net_worth_cents?: Maskable;
  readonly household_net_worth_cents?: Maskable;
  readonly monthly_income_cents?: Maskable;
  readonly monthly_expense_cents?: Maskable;
  readonly as_of_date?: string;
  readonly source_system?: string;
}

export interface CreditProfileData {
  readonly customer_id: string;
  readonly fico_score?: Maskable;
  readonly behavior_score?: Maskable;
  readonly propensity_score?: Maskable;
  readonly credit_utilization_bps?: Maskable;
  readonly years_on_bureau?: number | null;
  readonly num_inquiries?: number | null;
  readonly num_trades?: number | null;
  readonly num_credit_accounts?: number | null;
  readonly credit_exposure_cents?: Maskable;
  readonly as_of_date?: string;
  readonly source_system?: string;
}

export interface AccountData {
  readonly account_id: string;
  readonly account_number?: Maskable;
  readonly account_type: string;
  readonly product_name?: string | null;
  readonly product_code?: string | null;
  readonly balance_cents?: Maskable;
  readonly available_balance_cents?: Maskable;
  readonly interest_rate_bps?: Maskable;
  readonly account_status: string;
  readonly open_date?: string;
  readonly close_date?: string | null;
  readonly as_of_date?: string;
  readonly source_system?: string;
}

export interface DepositDetail {
  readonly kind: 'DEPOSIT';
  readonly product_type?: string | null;
  readonly household_deposits_cents?: Maskable;
  readonly maturity_date?: string | null;
}

export interface LoanDetail {
  readonly kind: 'LOAN';
  readonly loan_number?: Maskable;
  readonly loan_type?: string | null;
  readonly original_amount_cents?: Maskable;
  readonly monthly_emi_cents?: Maskable;
  readonly loan_status?: string | null;
  readonly loan_start_date?: string | null;
  readonly loan_end_date?: string | null;
  readonly collateral_asset_id?: string | null;
}

export interface CardDetail {
  readonly kind: 'CARD';
  readonly card_last4?: Maskable;
  readonly card_type?: string | null;
  readonly credit_limit_cents?: Maskable;
  readonly utilization_bps?: Maskable;
  readonly rewards_balance_cents?: Maskable;
  readonly monthly_spend_cents?: Maskable;
  readonly overlimit_events?: number;
  readonly fraud_alerts?: number;
}

export interface InvestmentDetail {
  readonly kind: 'INVESTMENT';
  readonly portfolio_value_cents?: Maskable;
  readonly asset_allocation?: string | null;
  readonly mutual_funds_cents?: Maskable;
  readonly stocks_cents?: Maskable;
  readonly bonds_cents?: Maskable;
  readonly retirement_accounts_cents?: Maskable;
  readonly investment_risk_profile?: string | null;
}

export type ProductDetailData = DepositDetail | LoanDetail | CardDetail | InvestmentDetail;

export interface HoldingData {
  readonly account: AccountData;
  readonly detail?: ProductDetailData | null;
}

export interface HoldingsData {
  readonly holdings?: readonly HoldingData[];
}

export interface CategoryTotalData {
  readonly transaction_category: string;
  readonly total_cents: number;
  readonly transaction_count: number;
}

export interface MonthlyFlagData {
  readonly month: string;
  readonly total_cents: number;
  readonly transaction_count: number;
  readonly is_anomaly: boolean;
  readonly deviation_sigma?: number | null;
}

export interface ExpenseAnalyticsData {
  readonly by_category?: readonly CategoryTotalData[];
  readonly monthly?: readonly MonthlyFlagData[];
  readonly threshold_sigma?: number;
}

export interface RiskProfileData {
  readonly customer_id: string;
  readonly risk_score?: Maskable;
  readonly fraud_score?: Maskable;
  readonly pid_score?: Maskable;
  readonly sid_score?: Maskable;
  readonly delinquency_status?: Maskable;
  readonly current_days_past_due?: Maskable;
  readonly default_indicator?: Maskable;
  readonly chargeoff_indicator?: Maskable;
  readonly aml_flag?: Maskable;
  readonly pep_flag?: Maskable;
  readonly as_of_date?: string;
  readonly source_system?: string;
}

export interface RiskAlertData {
  readonly category: string;
  readonly severity: number;
  readonly detail: string;
  readonly dismissible: boolean;
}

export interface RiskData {
  readonly profile: RiskProfileData;
  readonly band: string;
  readonly credit_exposure_cents?: Maskable;
  readonly alerts?: readonly RiskAlertData[];
  readonly requires_compliance_indicator: boolean;
}

export interface OfferData {
  readonly rank: number;
  readonly expected_value_cents: number;
  readonly is_cross_sell: boolean;
  readonly is_upsell: boolean;
  readonly rationale: string;
  readonly suppressed: boolean;
  readonly suppression_reason?: string | null;
  readonly offer_id: string;
  readonly offer_name: string;
  readonly business_group: string;
  readonly offer_type: string;
}

export interface OffersData {
  readonly offers?: readonly OfferData[];
  readonly campaign_ids?: readonly string[];
}

export type TimelineCategory =
  'LIFE_EVENT' | 'APPLICATION' | 'MAJOR_TRANSACTION' | 'RELATIONSHIP_CHANGE';

export interface TimelineEntryData {
  readonly category: TimelineCategory;
  readonly entry_date: string;
  readonly title: string;
  readonly source_id: string;
  readonly amount_cents?: number | null;
}

export interface TimelineData {
  readonly timeline?: readonly TimelineEntryData[];
}

export interface HouseholdData {
  readonly household_id: string;
  readonly household_name?: string;
  readonly primary_customer_id?: string | null;
  readonly member_count?: number;
  readonly as_of_date?: string;
  readonly source_system?: string;
}

export interface HouseholdRollupData {
  readonly net_worth_cents?: Maskable;
  readonly total_deposits_cents?: Maskable;
  readonly product_count?: number;
  readonly member_count?: number;
}

export interface GraphNodeData {
  readonly node_id: string;
  readonly node_type: string;
  readonly entity_id: string;
  readonly label: string;
  readonly props: Record<string, unknown>;
}

export interface GraphEdgeData {
  readonly src_id: string;
  readonly dst_id: string;
  readonly edge_type: string;
  readonly is_inferred: boolean;
  readonly confidence?: number | null;
}

export interface RelationshipData {
  readonly relationship_id: string;
  readonly from_customer_id: string;
  readonly to_customer_id: string;
  readonly relationship_type: string;
  readonly is_inferred: boolean;
  readonly confidence?: number | null;
  readonly inference_basis?: string | null;
}

export interface RelationshipsData {
  readonly relationships?: readonly RelationshipData[];
  readonly nodes?: readonly GraphNodeData[];
  readonly edges?: readonly GraphEdgeData[];
}

export interface EngagementEventData {
  readonly event_id: string;
  readonly event_type: string;
  readonly event_date: string;
  readonly channel: string;
  readonly session_id?: string | null;
  readonly device_type?: string | null;
  readonly outcome?: string | null;
  readonly notes?: string | null;
}

export interface EngagementData {
  readonly events?: readonly EngagementEventData[];
}

/** Whether a graph node is a redacted, out-of-book customer (structure-only). */
export function isRestrictedNode(node: GraphNodeData): boolean {
  return node.props.restricted === true || node.entity_id === '';
}

/**
 * Read one module slice out of the composed 360 payload as its refined widget shape.
 *
 * The payload is an open `dict[str, Any]` on the wire, so a widget names the slot it wants and the
 * expected shape here rather than indexing the loose object inline (which reads as an untyped
 * string-key access). The value is `undefined` when the module was absent — failed, or empty.
 */
export function pick<T>(raw: Record<string, unknown>, slot: string): T | undefined {
  return raw[slot] as T | undefined;
}
