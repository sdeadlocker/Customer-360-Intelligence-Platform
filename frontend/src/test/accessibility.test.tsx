import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Envelope, Meta } from '../api/types';
import type { Dashboard360 } from '../features/dashboard/dashboardData';
import { FilterProvider } from '../features/dashboard/filters';
import {
  ExpenseCategoryWidget,
  ExpenseTrendWidget,
  ExpenseWidget,
} from '../features/dashboard/widgets/ExpenseWidget';
import { OffersWidget } from '../features/dashboard/widgets/OffersWidget';
import { RelationshipWidget } from '../features/dashboard/widgets/RelationshipWidget';
import { RiskWidget } from '../features/dashboard/widgets/RiskWidget';

/**
 * Automated accessibility gate (task 15.4, requirements 16.1, 16.3).
 *
 * axe-core runs against each rendered surface and asserts zero violations, so a regression that
 * removes a label, drops contrast below AA, or breaks the ARIA structure fails the build. This is
 * the machine half of the pass; the manual screen-reader findings live in `docs/accessibility.md`,
 * as axe cannot judge focus order, meaningful announcements or "no colour-only" on its own.
 *
 * Each surface is exercised in the state the phase gate names: the charts with their table
 * equivalents shown, the relationship graph in both its diagram and its keyboard tree view.
 */

// ---------------------------------------------------------------- fixtures

const { getEnvelopeMock } = vi.hoisted(() => ({ getEnvelopeMock: vi.fn() }));

vi.mock('../api/client', () => ({
  getEnvelope: getEnvelopeMock,
  ApiError: class ApiError extends Error {
    correlationId: string | null = null;
  },
}));

function meta(overrides: Partial<Meta> = {}): Meta {
  return {
    correlation_id: 'c',
    trace_id: null,
    as_of: '2026-09-01T00:00:00Z',
    masked_fields: [],
    computed_fields: [],
    errors: [],
    ...overrides,
  };
}

function envelope<T>(data: T, metaOverrides: Partial<Meta> = {}): Envelope<T> {
  return { data, meta: meta(metaOverrides) };
}

function view(raw: Record<string, unknown>): Dashboard360 {
  const present = new Set(Object.keys(raw).filter((k) => k !== 'customer_id'));
  return {
    customerId: 'C1',
    present,
    maskedFields: [],
    errors: [],
    raw: { customer_id: 'C1', ...raw },
  };
}

function withFilters(node: React.ReactNode): React.JSX.Element {
  return <FilterProvider>{node}</FilterProvider>;
}

beforeEach(() => {
  getEnvelopeMock.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------- charts + gauges

describe('accessibility — chart-bearing widgets', () => {
  it('the expense widget has no violations with both tables shown', async () => {
    const v = view({
      expense_analytics: {
        threshold_sigma: 2,
        by_category: [
          { transaction_category: 'GROCERIES', total_cents: 120000, transaction_count: 40 },
          { transaction_category: 'TRAVEL', total_cents: 60000, transaction_count: 8 },
        ],
        monthly: [
          {
            month: '2026-06',
            total_cents: 300000,
            transaction_count: 50,
            is_anomaly: false,
            deviation_sigma: 0.3,
          },
          {
            month: '2026-07',
            total_cents: 620000,
            transaction_count: 60,
            is_anomaly: true,
            deviation_sigma: 2.6,
          },
        ],
      },
    });

    // The expense charts are their own cards now; render the summary + both chart cards.
    const { container } = render(
      withFilters(
        <>
          <ExpenseWidget view={v} />
          <ExpenseCategoryWidget view={v} />
          <ExpenseTrendWidget view={v} />
        </>,
      ),
    );
    // Reveal both table equivalents so axe sees the tables, not just the aria-hidden charts.
    for (const toggle of screen.getAllByRole('button', { name: /show data table/i })) {
      await userEvent.click(toggle);
    }
    expect(await axe(container)).toHaveNoViolations();
  });

  it('the risk widget has no violations with the gauge table shown', async () => {
    const v = view({
      risk: {
        profile: {
          customer_id: 'C1',
          risk_score: 42,
          delinquency_status: 'CURRENT',
          as_of_date: '2026-09-01',
          source_system: 'CORE',
        },
        band: 'MODERATE',
        credit_exposure_cents: 500000,
        alerts: [{ category: 'AML', severity: 3, detail: 'Review required', dismissible: false }],
        requires_compliance_indicator: true,
      },
    });

    const { container } = render(<RiskWidget view={v} />);
    await userEvent.click(screen.getByRole('button', { name: /show data table/i }));
    expect(await axe(container)).toHaveNoViolations();
  });

  it('the offers widget has no violations with the offer table shown', async () => {
    const v = view({
      offers: {
        campaign_ids: ['CMP1'],
        offers: [
          {
            offer_id: 'O1',
            offer_name: 'Premium card',
            business_group: 'CARDS',
            expected_value_cents: 250000,
            rank: 1,
            is_cross_sell: true,
            is_upsell: false,
            suppressed: false,
            suppression_reason: null,
            rationale: 'Strong fit; high confidence.',
          },
          {
            offer_id: 'O2',
            offer_name: 'Personal loan',
            business_group: 'LENDING',
            expected_value_cents: 90000,
            rank: 2,
            is_cross_sell: false,
            is_upsell: true,
            suppressed: true,
            suppression_reason: 'Cooling-off window',
            rationale: 'Recently declined; low confidence.',
          },
        ],
      },
    });

    const { container } = render(<OffersWidget view={v} />);
    await userEvent.click(screen.getByRole('button', { name: /show data table/i }));
    expect(await axe(container)).toHaveNoViolations();
  });
});

// ---------------------------------------------------------------- relationship graph + tree

describe('accessibility — relationship network', () => {
  function mockGraph(): void {
    getEnvelopeMock.mockImplementation((path: string) => {
      if (path.includes('/relationships')) {
        return Promise.resolve(
          envelope({
            relationships: [],
            nodes: [
              {
                node_id: 'CUSTOMER:C1',
                node_type: 'Customer',
                entity_id: 'C1',
                label: 'In view',
                props: {},
              },
              {
                node_id: 'CUSTOMER:C2',
                node_type: 'Customer',
                entity_id: 'C2',
                label: 'Neighbour',
                props: {},
              },
              {
                node_id: 'CUSTOMER:restricted',
                node_type: 'Customer',
                entity_id: '',
                label: 'Restricted',
                props: { restricted: true },
              },
            ],
            edges: [
              {
                src_id: 'CUSTOMER:C1',
                dst_id: 'CUSTOMER:C2',
                edge_type: 'HOUSEHOLD_MEMBER',
                is_inferred: false,
                confidence: null,
              },
              {
                src_id: 'CUSTOMER:C1',
                dst_id: 'CUSTOMER:restricted',
                edge_type: 'RELATED_TO',
                is_inferred: true,
                confidence: 0.8,
              },
            ],
          }),
        );
      }
      return Promise.resolve(envelope({ household: null }));
    });
  }

  it('has no violations in the diagram view', async () => {
    mockGraph();
    const { container } = render(
      <MemoryRouter>
        <RelationshipWidget customerId="C1" />
      </MemoryRouter>,
    );
    await screen.findByRole('group', { name: /relationship network diagram/i });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('has no violations in the keyboard tree view and exposes an ARIA tree', async () => {
    mockGraph();
    render(
      <MemoryRouter>
        <RelationshipWidget customerId="C1" />
      </MemoryRouter>,
    );
    const treeToggle = await screen.findByRole('button', { name: /^tree$/i });
    await userEvent.click(treeToggle);

    const tree = screen.getByRole('tree', { name: /relationship network/i });
    expect(tree).toBeInTheDocument();
    // The focus root and its neighbours are all present as tree items — the identical node set.
    const items = screen.getAllByRole('treeitem');
    expect(items.length).toBe(3);
    expect(await axe(tree)).toHaveNoViolations();
  });

  it('drives the node inspector from the tree via the keyboard', async () => {
    mockGraph();
    render(
      <MemoryRouter>
        <RelationshipWidget customerId="C1" />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole('button', { name: /^tree$/i }));

    // The root is the single tab stop; arrow down to the neighbour and select with Enter.
    const root = screen.getByRole('treeitem', { name: /focus/i });
    root.focus();
    await userEvent.keyboard('{ArrowDown}{Enter}');
    expect(screen.getByRole('group', { name: /node detail/i })).toBeInTheDocument();
  });
});
