import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { Dashboard360 } from '../dashboardData';
import { FilterProvider } from '../filters';
import { ContactWidget } from './ContactWidget';
import { ExpenseCategoryWidget, ExpenseTrendWidget, ExpenseWidget } from './ExpenseWidget';
import { JourneyWidget } from './JourneyWidget';
import { OffersWidget } from './OffersWidget';
import { ProfileWidget } from './ProfileWidget';
import { RiskWidget } from './RiskWidget';

/**
 * Unit tests for the Phase 13 widgets that read from the composed 360 view. Each asserts the
 * behaviour the task calls out — masking pass-through, restricted state, deviation flags, offer
 * suppression, the non-dismissible compliance banner — against a hand-built `Dashboard360`.
 */

function view(raw: Record<string, unknown>, meta: Partial<Dashboard360> = {}): Dashboard360 {
  const present = new Set(Object.keys(raw).filter((k) => k !== 'customer_id'));
  return {
    customerId: 'C1',
    present,
    maskedFields: [],
    errors: [],
    raw: { customer_id: 'C1', ...raw },
    ...meta,
  };
}

function withFilters(node: React.ReactNode): React.JSX.Element {
  return <FilterProvider>{node}</FilterProvider>;
}

describe('ProfileWidget', () => {
  it('renders profile fields and passes a masked name string through unchanged', () => {
    const v = view(
      {
        profile: {
          customer_id: 'C1',
          customer_name: 'Renata ***',
          customer_type: 'INDIVIDUAL',
          customer_segment: 'MASS',
          customer_since: '2015-03-01',
          customer_value: 'MEDIUM',
        },
      },
      { maskedFields: ['profile.customer_name'] },
    );
    render(<ProfileWidget view={v} />);
    // The masked name is shown verbatim; the module is flagged restricted.
    expect(screen.getByText('Renata ***')).toBeInTheDocument();
    expect(screen.getByText('Restricted')).toBeInTheDocument();
  });
});

describe('ContactWidget', () => {
  it('is restricted when contact fields are masked and shows the surviving city', () => {
    const v = view(
      { contact: { customer_id: 'C1', city: 'London', email: '***' } },
      { maskedFields: ['contact.email'] },
    );
    render(<ContactWidget view={v} />);
    expect(screen.getByText('London')).toBeInTheDocument();
    expect(screen.getByText('Restricted')).toBeInTheDocument();
  });
});

describe('RiskWidget', () => {
  it('shows a non-dismissible compliance banner when AML/PEP is flagged', () => {
    const v = view({
      risk: {
        profile: { customer_id: 'C1', aml_flag: true, pep_flag: false },
        band: 'HIGH',
        credit_exposure_cents: 100000,
        alerts: [{ category: 'COMPLIANCE', severity: 3, detail: 'AML review', dismissible: false }],
        requires_compliance_indicator: true,
      },
    });
    render(<RiskWidget view={v} />);
    const banner = screen.getByRole('alert');
    expect(banner).toHaveTextContent(/cannot be dismissed/i);
    expect(screen.getByText(/Risk band:/)).toHaveTextContent('High');
  });

  it('shows only the coarse band when the score is masked to a band string', () => {
    const v = view({
      risk: {
        profile: { customer_id: 'C1', risk_score: 'MEDIUM', aml_flag: false, pep_flag: false },
        band: 'MODERATE',
        credit_exposure_cents: 0,
        alerts: [],
        requires_compliance_indicator: false,
      },
    });
    render(<RiskWidget view={v} />);
    // The band renders; the masked driver value is shown verbatim, not as a number.
    expect(screen.getByText(/Risk band:/)).toHaveTextContent('Moderate');
    expect(screen.getByText('MEDIUM')).toBeInTheDocument();
  });
});

describe('OffersWidget', () => {
  it('distinguishes cross-sell and upsell and greys a suppressed offer with its reason', () => {
    const v = view({
      offers: {
        offers: [
          {
            rank: 1,
            expected_value_cents: 500000,
            is_cross_sell: true,
            is_upsell: false,
            rationale: 'Product affinity, high confidence',
            suppressed: false,
            offer_id: 'O1',
            offer_name: 'Premium Card',
            business_group: 'CARDS',
            offer_type: 'CROSS_SELL',
          },
          {
            rank: 2,
            expected_value_cents: 100000,
            is_cross_sell: false,
            is_upsell: true,
            rationale: 'Upgrade path, low confidence',
            suppressed: true,
            suppression_reason: 'COOLING_OFF: declined recently',
            offer_id: 'O2',
            offer_name: 'Gold Upgrade',
            business_group: 'CARDS',
            offer_type: 'UPSELL',
          },
        ],
        campaign_ids: ['CAMP-1'],
      },
    });
    render(<OffersWidget view={v} />);
    // Scope to the offer badges: "Cross-sell"/"Upsell" also appear as filter-dropdown options, so
    // an unscoped text query is ambiguous.
    expect(document.querySelector('.offer__kind--crosssell')).toHaveTextContent('Cross-sell');
    expect(document.querySelector('.offer__kind--upsell')).toHaveTextContent('Upsell');
    expect(screen.getByText('Suppressed')).toBeInTheDocument();
    // The raw COOLING_OFF: enum prefix is humanized for display; the detail after the colon stays.
    expect(screen.getByText(/Cooling off: declined recently/)).toBeInTheDocument();
    expect(screen.getByText(/Campaigns:/)).toHaveTextContent('CAMP-1');
  });
});

describe('ExpenseWidget', () => {
  it('flags a deviating month and offers an accessible data table', async () => {
    const user = userEvent.setup();
    const v = view({
      expense_analytics: {
        by_category: [
          { transaction_category: 'DINING', total_cents: 50000, transaction_count: 12 },
        ],
        monthly: [
          {
            month: '2026-01',
            total_cents: 100000,
            transaction_count: 20,
            is_anomaly: false,
            deviation_sigma: 0.2,
          },
          {
            month: '2026-02',
            total_cents: 400000,
            transaction_count: 40,
            is_anomaly: true,
            deviation_sigma: 3.1,
          },
        ],
        threshold_sigma: 2,
      },
    });
    // The expense charts are now their own cards; render the trend and category cards together.
    render(
      withFilters(
        <>
          <ExpenseWidget view={v} />
          <ExpenseCategoryWidget view={v} />
          <ExpenseTrendWidget view={v} />
        </>,
      ),
    );

    // The flagged month carries a signed, worded deviation badge (not colour alone).
    expect(screen.getByText(/High \+3\.1σ/)).toBeInTheDocument();

    // Each chart card has a "Show data table" toggle (requirement 16.2). The category card renders
    // first, so its toggle is the first one; opening it reveals the category table.
    const toggles = screen.getAllByRole('button', { name: /show data table/i });
    expect(toggles.length).toBeGreaterThanOrEqual(2);
    await user.click(toggles[0]!);
    expect(screen.getByRole('table', { name: /spend by category/i })).toBeInTheDocument();
  });
});

describe('JourneyWidget', () => {
  it('renders a zoomable timeline and opens a milestone detail on selection', async () => {
    const user = userEvent.setup();
    const v = view({
      timeline: {
        timeline: [
          {
            category: 'LIFE_EVENT',
            entry_date: '2020-06-01',
            title: 'MARRIAGE',
            source_id: 'LE-1',
          },
          {
            category: 'MAJOR_TRANSACTION',
            entry_date: '2021-01-15',
            title: 'AUTO: Dealer',
            source_id: 'TX-1',
            amount_cents: 3500000,
          },
        ],
      },
    });
    render(withFilters(<JourneyWidget view={v} />));

    expect(screen.getByRole('button', { name: /zoom in/i })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /MARRIAGE/i }));
    const detail = screen.getByRole('group', { name: /milestone detail/i });
    expect(within(detail).getByText('LE-1')).toBeInTheDocument();
  });
});
