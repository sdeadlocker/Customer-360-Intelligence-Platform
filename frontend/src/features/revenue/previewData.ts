import type {
  EconomicProfit,
  FeeRecovery,
  MoneyInMotionEvent,
  OpportunityPipeline,
  WalletShare,
} from './revenueApi';

/**
 * Illustrative fixtures for the revenue plays, used **only** when the backend endpoint is absent.
 *
 * The revenue engine (detection job + endpoints) is not built yet, so every revenue hook degrades to
 * a `preview` state on a failed fetch and renders these figures behind a visible **Preview** badge
 * and an explicit "illustrative figures" note. This keeps the platform's honesty rule intact — a
 * number on screen is either grounded in the customer database or is unmistakably labelled as a
 * layout preview — while letting the UI be reviewed and demonstrated before the backend exists.
 *
 * Delete this file and the `preview` branch of {@link ./useRevenue} the moment the endpoints land;
 * nothing else references it.
 */

export const PREVIEW_PIPELINE: OpportunityPipeline = {
  identified_cents: 4_182_000_00,
  capturable_cents: 1_940_500_00,
  realized_cents: 421_800_00,
  realization_bps: 2174,
  urgent_count: 3,
  book_size: 100,
  as_of: '2026-09-16',
  source_system: 'PREVIEW',
  plays: [
    {
      play: 'HELD_AWAY_CAPTURE',
      identified_cents: 2_610_000_00,
      capturable_cents: 1_044_000_00,
      realized_cents: 187_200_00,
      opportunity_count: 34,
    },
    {
      play: 'DEPOSIT_RETENTION',
      identified_cents: 968_000_00,
      capturable_cents: 561_000_00,
      realized_cents: 142_600_00,
      opportunity_count: 21,
    },
    {
      play: 'MONEY_IN_MOTION',
      identified_cents: 517_000_00,
      capturable_cents: 271_500_00,
      realized_cents: 68_400_00,
      opportunity_count: 9,
    },
    {
      play: 'FEE_RECOVERY',
      identified_cents: 87_000_00,
      capturable_cents: 64_000_00,
      realized_cents: 23_600_00,
      opportunity_count: 47,
    },
  ],
};

export const PREVIEW_INFLOWS: readonly MoneyInMotionEvent[] = [
  {
    event_id: 9001,
    customer_id: 'C00042',
    customer_label: 'R. Alvarez',
    amount_cents: 248_000_00,
    inferred_source: 'PROPERTY_SALE',
    confidence: 0.86,
    detected_at: '2026-09-16T07:12:00Z',
    days_remaining: 1,
    recommended_action: 'Call today — position the money-market ladder before funds are swept out.',
    evidence: {
      summary:
        'Single inbound wire of $248,000 from a title company, 41× the customer’s median credit.',
      details: { Channel: 'Wire in', Pattern: 'One-off', Segment: 'Affluent' },
    },
    status: 'NEW',
  },
  {
    event_id: 9002,
    customer_id: 'C00117',
    customer_label: 'D. Okafor',
    amount_cents: 96_500_00,
    inferred_source: 'BUSINESS_EXIT',
    confidence: 0.71,
    detected_at: '2026-09-15T15:40:00Z',
    days_remaining: 3,
    recommended_action:
      'Introduce wealth advisory; business operating account balance also spiked.',
    evidence: {
      summary: 'Inbound ACH of $96,500 from an escrow agent, followed by payroll ceasing.',
      details: { Channel: 'ACH in', Pattern: 'One-off', Segment: 'Small business' },
    },
    status: 'NEW',
  },
  {
    event_id: 9003,
    customer_id: 'C00088',
    customer_label: 'M. Lindqvist',
    amount_cents: 41_200_00,
    inferred_source: 'BONUS',
    confidence: 0.93,
    detected_at: '2026-09-14T09:02:00Z',
    days_remaining: 6,
    recommended_action: 'Offer the 11-month CD; last two annual bonuses left within nine days.',
    evidence: {
      summary: 'Salary credit 4.2× the trailing monthly average from the same employer.',
      details: { Channel: 'Direct deposit', Pattern: 'Annual', Segment: 'Mass affluent' },
    },
    status: 'NEW',
  },
];

export const PREVIEW_WALLET_SHARE: WalletShare = {
  internal_value_cents: 312_400_00,
  held_away_cents: 843_000_00,
  capturable_cents: 310_000_00,
  annual_revenue_if_captured_cents: 8_450_00,
  wallet_share_bps: 2704,
  is_primary_bank: false,
  as_of_date: '2026-09-16',
  source_system: 'PREVIEW',
  holdings: [
    {
      holding_id: 1,
      holding_type: 'BROKERAGE',
      counterparty: 'External brokerage',
      estimated_value_cents: 512_000_00,
      monthly_flow_cents: 4_000_00,
      confidence: 0.82,
      basis: '18 consecutive monthly ACH debits of $4,000 to a brokerage counterparty.',
    },
    {
      holding_id: 2,
      holding_type: 'MORTGAGE',
      counterparty: 'External mortgage servicer',
      estimated_value_cents: 331_000_00,
      monthly_flow_cents: 2_100_00,
      confidence: 0.88,
      basis: 'Fixed $2,100 monthly debit to a loan servicer with no mortgage on our books.',
    },
    {
      holding_id: 3,
      holding_type: 'PAYROLL',
      counterparty: 'Another institution',
      estimated_value_cents: null,
      monthly_flow_cents: 9_400_00,
      confidence: 0.95,
      basis: 'Salary arrives as an inbound transfer, not a direct deposit — we are not primary.',
    },
  ],
};

/**
 * Fee recovery is the one play with a real backend, so this fixture is reached only when that
 * endpoint is unreachable. It is kept in the live contract's shape — same rule bases, same document
 * ids, same evidence style the scan emits — so the preview cannot drift into describing a response
 * the API no longer returns.
 */
export const PREVIEW_FEE_RECOVERY: FeeRecovery = {
  monthly_recoverable_cents: 62_00,
  annualized_recoverable_cents: 744_00,
  as_of_date: '2026-09-01',
  source_system: 'PREVIEW',
  findings: [
    {
      finding_id: 2420027828,
      leak_type: 'MISSED_MINIMUM',
      account_label: 'Premier Checking ****0705',
      monthly_cents: 30_00,
      annualized_cents: 360_00,
      rule_basis: 'Premier Checking - Product Sheet, Rates and Fees',
      doc_id: 'pc-dda-premier',
      evidence:
        '12 of the last 12 cycles did not qualify for a waiver (waived by relationship balance ' +
        "of $25,000) yet no maintenance fee was posted. Balance tested against the account's " +
        'as-of balance, the only balance the source data carries.',
      cycles: 12,
      status: 'OPEN',
    },
    {
      finding_id: 1174391044,
      leak_type: 'FEE_WAIVED',
      account_label: 'Money Market Select ****9013',
      monthly_cents: 12_00,
      annualized_cents: 144_00,
      rule_basis: 'Fee Waiver Authority Policy, Eligibility to Waive',
      doc_id: 'pol-fee-waiver-authority',
      evidence:
        'The maintenance fee was posted and reversed in 11 consecutive cycles. The first 3 are ' +
        'treated as service recovery — a configured allowance, not a published rule — leaving 8 ' +
        'cycles to review.',
      cycles: 8,
      status: 'OPEN',
    },
    {
      finding_id: 3312884736,
      leak_type: 'UNBILLED_SERVICE',
      account_label: 'Business Operating Account ****2277',
      monthly_cents: 16_00,
      annualized_cents: 192_00,
      rule_basis: 'Business Operating Account - Product Sheet, Rates and Fees',
      doc_id: 'pc-bus-chk',
      evidence:
        '8 of 23 outgoing wires were sent without the $25 per-wire fee the product sheet prices.',
      cycles: 8,
      status: 'OPEN',
    },
    {
      finding_id: 907542311,
      leak_type: 'LEGACY_PRICING',
      account_label: 'High Yield Savings ****7728',
      monthly_cents: 4_00,
      annualized_cents: 48_00,
      rule_basis: 'High Yield Savings - Product Sheet, Rates and Fees',
      doc_id: 'pc-sav-hiyield',
      evidence:
        '17 cycles were billed below the current schedule of $10, leaving $69 uncollected across ' +
        'the window.',
      cycles: 17,
      status: 'OPEN',
    },
  ],
};

export const PREVIEW_ECONOMIC_PROFIT: EconomicProfit = {
  economic_profit_cents: 1_284_00,
  profit_band: 'SOLID',
  balance_tier: 'PLATINUM',
  profit_tier: 'SILVER',
  tier_mismatch: true,
  segment_median_cents: 2_060_00,
  as_of_date: '2026-09-16',
  source_system: 'PREVIEW',
  components: [
    {
      key: 'nii_deposits',
      label: 'Deposit spread (NII)',
      amount_cents: 1_940_00,
      direction: 'CREDIT',
    },
    { key: 'nii_loans', label: 'Loan spread (NII)', amount_cents: 2_310_00, direction: 'CREDIT' },
    { key: 'fees', label: 'Fee income', amount_cents: 420_00, direction: 'CREDIT' },
    { key: 'interchange', label: 'Card interchange', amount_cents: 268_00, direction: 'CREDIT' },
    { key: 'cost_to_serve', label: 'Cost to serve', amount_cents: 1_150_00, direction: 'DEBIT' },
    {
      key: 'expected_loss',
      label: 'Expected credit loss',
      amount_cents: 1_640_00,
      direction: 'DEBIT',
    },
    { key: 'cost_of_capital', label: 'Cost of capital', amount_cents: 864_00, direction: 'DEBIT' },
  ],
};
