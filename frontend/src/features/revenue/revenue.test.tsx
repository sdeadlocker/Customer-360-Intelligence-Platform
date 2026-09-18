import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Envelope, Meta, Page } from '../../api/types';
import type {
  EconomicProfit,
  FeeRecovery,
  MoneyInMotionEvent,
  OpportunityPipeline,
  WalletShare,
} from './revenueApi';

/**
 * Tests for the four revenue plays (Phase 22).
 *
 * The revenue hooks read through the generic envelope helpers, so those are mocked (the `vi.hoisted`
 * pattern from `SignalsWorklist.test.tsx`) and every case drives the UI without a backend. Two states
 * matter and both are covered per surface:
 *
 *  - **ready** — the endpoint answered; the figures on screen are the server's.
 *  - **preview** — the endpoint is absent (it is not built yet), so the card renders illustrative
 *    fixtures and *must* show the Preview marker. A regression that silently drops that marker would
 *    let a layout preview pass as customer data, so it is asserted explicitly.
 *
 * The masking discipline is asserted too: a banded string must render verbatim and must not be
 * charted, because a band cannot be plotted to scale.
 */

const { getEnvelopeMock, postEnvelopeMock } = vi.hoisted(() => ({
  getEnvelopeMock: vi.fn(),
  postEnvelopeMock: vi.fn(),
}));

vi.mock('../../api/client', () => ({
  getEnvelope: getEnvelopeMock,
  postEnvelope: postEnvelopeMock,
  ApiError: class ApiError extends Error {
    code = 'X';
    correlationId = null;
    status = 500;
  },
}));

import { EconomicProfitWidget } from './EconomicProfitWidget';
import { FeeRecoveryWidget } from './FeeRecoveryWidget';
import { MoneyInMotionPanel, MoneyInMotionWidget } from './MoneyInMotion';
import { RevenueHero } from './RevenueHero';
import { WalletShareWidget } from './WalletShareWidget';

const meta: Meta = {
  correlation_id: 'c',
  trace_id: 't',
  as_of: '2026-09-16T00:00:00Z',
  masked_fields: [],
  computed_fields: [],
  errors: [],
};

function envelope<T>(data: T): Envelope<T> {
  return { data, meta };
}

function page<T>(items: readonly T[]): Envelope<Page<T>> {
  return { data: { items: [...items], next_cursor: null }, meta };
}

// ---------------------------------------------------------------- fixtures

function pipeline(overrides: Partial<OpportunityPipeline> = {}): OpportunityPipeline {
  return {
    identified_cents: 1_000_000_00,
    capturable_cents: 400_000_00,
    realized_cents: 100_000_00,
    realization_bps: 2500,
    urgent_count: 2,
    book_size: 100,
    as_of: '2026-09-16',
    source_system: 'DERIVED',
    plays: [
      {
        play: 'HELD_AWAY_CAPTURE',
        identified_cents: 700_000_00,
        capturable_cents: 300_000_00,
        realized_cents: 80_000_00,
        opportunity_count: 12,
      },
      {
        play: 'FEE_RECOVERY',
        identified_cents: 300_000_00,
        capturable_cents: 100_000_00,
        realized_cents: 20_000_00,
        opportunity_count: 31,
      },
    ],
    ...overrides,
  };
}

function inflow(overrides: Partial<MoneyInMotionEvent> = {}): MoneyInMotionEvent {
  return {
    event_id: 1,
    customer_id: 'C-1',
    customer_label: 'R. Alvarez',
    amount_cents: 250_000_00,
    inferred_source: 'PROPERTY_SALE',
    confidence: 0.9,
    detected_at: '2026-09-16T00:00:00Z',
    days_remaining: 5,
    recommended_action: 'Call before the funds are swept out.',
    evidence: { summary: 'Inbound wire 40x the median credit.', details: {} },
    status: 'NEW',
    ...overrides,
  };
}

function walletShare(overrides: Partial<WalletShare> = {}): WalletShare {
  return {
    internal_value_cents: 100_000_00,
    held_away_cents: 300_000_00,
    capturable_cents: 120_000_00,
    annual_revenue_if_captured_cents: 900_00,
    wallet_share_bps: 2500,
    is_primary_bank: false,
    as_of_date: '2026-09-16',
    source_system: 'DERIVED',
    holdings: [
      {
        holding_id: 1,
        holding_type: 'MORTGAGE',
        counterparty: 'External mortgage servicer',
        estimated_value_cents: 300_000_00,
        monthly_flow_cents: 2_100_00,
        confidence: 0.88,
        basis: 'Fixed $2,100 monthly debit to a loan servicer with no mortgage on our books.',
      },
    ],
    ...overrides,
  };
}

function feeRecovery(overrides: Partial<FeeRecovery> = {}): FeeRecovery {
  return {
    monthly_recoverable_cents: 200_00,
    annualized_recoverable_cents: 2_400_00,
    as_of_date: '2026-09-16',
    source_system: 'DERIVED',
    findings: [
      {
        finding_id: 1,
        leak_type: 'UNBILLED_SERVICE',
        account_label: 'Business Operating Account ****2277',
        monthly_cents: 200_00,
        annualized_cents: 2_400_00,
        rule_basis: 'Business Operating Account - Product Sheet, Rates and Fees',
        doc_id: 'pc-bus-chk',
        evidence: '8 of 23 outgoing wires were sent without the $25 per-wire fee.',
        cycles: 8,
        status: 'OPEN',
      },
    ],
    ...overrides,
  };
}

function economicProfit(overrides: Partial<EconomicProfit> = {}): EconomicProfit {
  return {
    economic_profit_cents: 1_200_00,
    profit_band: 'SOLID',
    balance_tier: 'PLATINUM',
    profit_tier: 'SILVER',
    tier_mismatch: true,
    segment_median_cents: 2_000_00,
    as_of_date: '2026-09-16',
    source_system: 'DERIVED',
    components: [
      { key: 'nii', label: 'Deposit spread (NII)', amount_cents: 3_000_00, direction: 'CREDIT' },
      { key: 'ecl', label: 'Expected credit loss', amount_cents: 1_800_00, direction: 'DEBIT' },
    ],
    ...overrides,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------- play 8: pipeline hero

describe('RevenueHero — opportunity pipeline', () => {
  it('renders the pipeline KPIs and the per-play table equivalent', async () => {
    getEnvelopeMock.mockResolvedValue(envelope(pipeline()));

    render(<RevenueHero onOpenUrgent={vi.fn()} />);

    // Compact money labels: $400,000 -> $400K, and the realization rate as a whole percentage.
    // Both are unique to the KPI tiles; the word "Capturable" deliberately also appears in the bar
    // legend, so the tile is identified by its figure instead.
    expect(await screen.findByText('$400K')).toBeInTheDocument();
    expect(screen.getByText('25%')).toBeInTheDocument();
    expect(screen.getByText('$1.0M')).toBeInTheDocument();

    // The bar chart must have a full data-table equivalent (requirement 16.2).
    await userEvent.click(screen.getByRole('button', { name: /show data table/i }));
    const table = screen.getByRole('table', { name: /opportunity by revenue play/i });
    expect(within(table).getByRole('rowheader', { name: 'Held-away capture' })).toBeInTheDocument();
    expect(within(table).getByRole('rowheader', { name: 'Fee recovery' })).toBeInTheDocument();
  });

  it('surfaces the urgent count and calls back to the money-in-motion feed', async () => {
    const onOpenUrgent = vi.fn();
    getEnvelopeMock.mockResolvedValue(envelope(pipeline({ urgent_count: 3 })));

    render(<RevenueHero onOpenUrgent={onOpenUrgent} />);

    expect(await screen.findByText(/3 opportunities need action today/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /review now/i }));
    expect(onOpenUrgent).toHaveBeenCalledTimes(1);
  });

  it('marks the panel as a preview when the endpoint is absent', async () => {
    getEnvelopeMock.mockRejectedValue(new Error('not implemented'));

    render(<RevenueHero onOpenUrgent={vi.fn()} />);

    // The Preview badge is the guard against a layout preview reading as real data.
    expect(await screen.findByText('Preview')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/preview figures/i);
  });
});

// ---------------------------------------------------------------- play 5: money in motion

describe('MoneyInMotionPanel', () => {
  it('ranks by closing window and states urgency in words, not colour alone', async () => {
    getEnvelopeMock.mockResolvedValue(
      page([
        inflow({ event_id: 1, days_remaining: 6, customer_id: 'C-6' }),
        inflow({ event_id: 2, days_remaining: 1, customer_id: 'C-1' }),
      ]),
    );

    render(<MoneyInMotionPanel onOpenCustomer={vi.fn()} />);

    const items = await screen.findAllByRole('listitem');
    // The soonest-closing window is first.
    expect(within(items[0]!).getByText('Act today')).toBeInTheDocument();
    expect(within(items[0]!).getByText(/1 day left in the window/i)).toBeInTheDocument();
    expect(within(items[1]!).getByText('Window open')).toBeInTheDocument();
    expect(within(items[1]!).getByText(/6 days left in the window/i)).toBeInTheDocument();
  });

  it('drills through to the customer 360', async () => {
    const onOpen = vi.fn();
    getEnvelopeMock.mockResolvedValue(page([inflow({ customer_id: 'C-42' })]));

    render(<MoneyInMotionPanel onOpenCustomer={onOpen} />);
    await screen.findByText(/inbound wire/i);
    await userEvent.click(screen.getByRole('button', { name: /open 360/i }));
    expect(onOpen).toHaveBeenCalledWith('C-42');
  });

  it('logs outreach, removing the event and posting to the revenue service', async () => {
    getEnvelopeMock.mockResolvedValue(page([inflow({ event_id: 7 })]));
    postEnvelopeMock.mockResolvedValue({ data: { event_id: 7, status: 'ACTIONED' }, meta });

    render(<MoneyInMotionPanel onOpenCustomer={vi.fn()} />);
    await screen.findByText(/inbound wire/i);
    await userEvent.click(screen.getByRole('button', { name: /log outreach/i }));

    await waitFor(() => {
      expect(screen.queryByText(/inbound wire/i)).toBeNull();
    });
    expect(postEnvelopeMock).toHaveBeenCalledWith('/revenue/money-in-motion/7/ack', undefined);
    expect(screen.getByRole('status')).toHaveTextContent('Outreach logged.');
  });

  it('reads the customer-scoped feed on the dashboard card', async () => {
    getEnvelopeMock.mockResolvedValue(page([inflow()]));

    render(<MoneyInMotionWidget customerId="C-9" />);
    await screen.findByText(/inbound wire/i);

    const paths = getEnvelopeMock.mock.calls.map((call) => String(call[0]));
    expect(paths).toContain('/customers/C-9/revenue/money-in-motion');
  });
});

// ---------------------------------------------------------------- play 4: wallet share

describe('WalletShareWidget', () => {
  it('sizes the held-away opportunity and shows the inference basis', async () => {
    getEnvelopeMock.mockResolvedValue(envelope(walletShare()));

    render(<WalletShareWidget customerId="C-1" />);

    // "$300K" is the held-away tile; the words "Held away" also label the donut slice and the table
    // row, which is the intended chart-plus-equivalent duplication, so the figure identifies it.
    expect(await screen.findByText('$300K')).toBeInTheDocument();
    expect(screen.getByText('External mortgage')).toBeInTheDocument();
    // An inferred holding is labelled as inferred and states the pattern it rests on.
    expect(screen.getByText('Inferred')).toBeInTheDocument();
    expect(screen.getByText(/no mortgage on our books/i)).toBeInTheDocument();
    expect(screen.getByText('88% confidence')).toBeInTheDocument();
    // Primacy held elsewhere is called out explicitly.
    expect(screen.getByText('Not primary')).toBeInTheDocument();
  });

  it('renders a banded value verbatim and refuses to chart it', async () => {
    getEnvelopeMock.mockResolvedValue(
      envelope(walletShare({ internal_value_cents: '$100K-$1M', wallet_share_bps: null })),
    );

    render(<WalletShareWidget customerId="C-1" />);

    // The server's band string is shown as-is; no arithmetic and no plotted slice.
    expect(await screen.findByText('$100K-$1M')).toBeInTheDocument();
    expect(
      screen.getByText(/composition is not shown because a value is banded/i),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- play 6: fee recovery

describe('FeeRecoveryWidget', () => {
  it('states the annualized recoverable amount and the rule each finding rests on', async () => {
    getEnvelopeMock.mockResolvedValue(envelope(feeRecovery()));

    render(<FeeRecoveryWidget customerId="C-1" />);

    expect(await screen.findByText('recoverable per year')).toBeInTheDocument();
    // The annualized total appears twice by design: as the headline and as the single by-cause bar.
    expect(screen.getAllByText('$2.4K').length).toBeGreaterThan(0);
    // Likewise the cause labels both the by-cause bar and the finding row.
    expect(screen.getAllByText('Unbilled service').length).toBeGreaterThan(0);
    // The rule and its document id are both shown, so a finding can be traced to the corpus.
    expect(
      screen.getByText(/Business Operating Account - Product Sheet, Rates and Fees/),
    ).toBeInTheDocument();
    expect(screen.getByText(/pc-bus-chk/)).toBeInTheDocument();
    // And the sentence that establishes the gap.
    expect(screen.getByText(/8 of 23 outgoing wires/)).toBeInTheDocument();
  });

  it('queues a finding for billing and announces that it is session-only', async () => {
    getEnvelopeMock.mockResolvedValue(envelope(feeRecovery()));

    render(<FeeRecoveryWidget customerId="C-1" />);
    await userEvent.click(await screen.findByRole('button', { name: /queue for billing/i }));

    expect(screen.getByText('Queued for billing')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/session only/i);
  });
});

// ---------------------------------------------------------------- play 8: economic profit

describe('EconomicProfitWidget', () => {
  it('calls out a balance-tier versus profit-tier mismatch in words', async () => {
    getEnvelopeMock.mockResolvedValue(envelope(economicProfit()));

    render(<EconomicProfitWidget customerId="C-1" />);

    expect(await screen.findByText('Mismatch')).toBeInTheDocument();
    expect(
      screen.getByText(/ranked platinum by balance but earns like silver/i),
    ).toBeInTheDocument();
    expect(screen.getByText('Balance tier')).toBeInTheDocument();
    expect(screen.getByText('Profit tier')).toBeInTheDocument();
  });

  it('shows the build-up with debits distinguished from credits in the table', async () => {
    getEnvelopeMock.mockResolvedValue(envelope(economicProfit()));

    render(<EconomicProfitWidget customerId="C-1" />);
    await userEvent.click(await screen.findByRole('button', { name: /show data table/i }));

    const table = screen.getByRole('table', { name: /economic profit build-up/i });
    // Direction is a word, never inferred from the sign of the number.
    expect(within(table).getByText('Subtracts')).toBeInTheDocument();
    expect(within(table).getByText('Adds')).toBeInTheDocument();
  });

  it('omits the mismatch verdict when the tiers agree', async () => {
    getEnvelopeMock.mockResolvedValue(
      envelope(economicProfit({ tier_mismatch: false, profit_tier: 'PLATINUM' })),
    );

    render(<EconomicProfitWidget customerId="C-1" />);
    await screen.findByText('Balance tier');
    expect(screen.queryByText('Mismatch')).toBeNull();
  });
});
