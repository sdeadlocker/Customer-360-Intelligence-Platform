import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';
import type { Envelope, Meta } from './api/types';

/**
 * The Phase 12 gate, end to end in jsdom: a user logs in as a seeded role, searches, and lands on a
 * dashboard shell whose modules show the correct states — a restricted field renders "Restricted", a
 * failed module renders "Partial", a present module renders ready, and an absent one shows "no data".
 */

function meta(overrides: Partial<Meta> = {}): Meta {
  return {
    correlation_id: 'corr-1',
    trace_id: null,
    as_of: '2026-09-12T00:00:00Z',
    masked_fields: [],
    computed_fields: [],
    errors: [],
    ...overrides,
  };
}

function envelope<T>(data: T, metaOverrides: Partial<Meta> = {}): Envelope<T> {
  return { data, meta: meta(metaOverrides) };
}

const ME = envelope({
  user_id: 'rm.taylor',
  role: 'RM',
  entitlement: { kind: 'BOOK', count: 40, segments: [] },
  knowledge_levels: [],
});

const TOKEN = envelope({
  access_token: 'access-xyz',
  refresh_token: 'refresh-xyz',
  token_type: 'Bearer',
  expires_in: 900,
});

const SEARCH = envelope({
  items: [
    {
      customer_id: 'CUST-00000001',
      customer_name: 'Ada Lovelace',
      customer_segment: 'HNW',
      city: 'London',
    },
  ],
  next_cursor: null,
});

// A 360 payload exercising every module state: present (profile), masked (contact), failed (risk),
// present (financial_profile), and absent (offers, timeline, household, expense_analytics).
const VIEW_360 = envelope(
  {
    customer_id: 'CUST-00000001',
    profile: {
      customer_id: 'CUST-00000001',
      customer_name: 'Ada Lovelace',
      customer_type: 'INDIVIDUAL',
      customer_segment: 'HNW',
      customer_since: '2015-03-01',
      customer_value: 'HIGH',
      preferred_language: 'en',
      as_of_date: '2026-09-01',
      source_system: 'CORE',
    },
    contact: { customer_id: 'CUST-00000001', city: 'London' },
    financial_profile: {
      customer_id: 'CUST-00000001',
      total_deposits_cents: 100000,
      total_loans_cents: 0,
      total_investments_cents: 5000000,
      total_assets_cents: 5100000,
      total_liabilities_cents: 0,
      net_worth_cents: 5100000,
      as_of_date: '2026-09-01',
      source_system: 'CORE',
    },
  },
  {
    masked_fields: ['contact.email', 'contact.phone_number'],
    errors: [{ module: 'risk', code: 'UPSTREAM_UNAVAILABLE', message: 'risk service unavailable' }],
  },
);

// Sub-resources the widgets fetch outside the composed 360 payload.
const CREDIT = envelope({ credit_profile: { customer_id: 'CUST-00000001', fico_score: 780 } });
const ACCOUNTS = envelope({
  holdings: [
    {
      account: {
        account_id: 'ACC-1',
        account_type: 'DEPOSIT',
        product_name: 'Savings',
        balance_cents: 100000,
        account_status: 'ACTIVE',
      },
      detail: { kind: 'DEPOSIT', product_type: 'SAVINGS' },
    },
  ],
});
const RELATIONSHIPS = envelope({
  relationships: [],
  nodes: [
    {
      node_id: 'CUSTOMER:CUST-00000001',
      node_type: 'Customer',
      entity_id: 'CUST-00000001',
      label: 'Ada Lovelace',
      props: {},
    },
    {
      node_id: 'CUSTOMER:CUST-00000002',
      node_type: 'Customer',
      entity_id: 'CUST-00000002',
      label: 'Charles Babbage',
      props: {},
    },
  ],
  edges: [
    {
      src_id: 'CUSTOMER:CUST-00000001',
      dst_id: 'CUSTOMER:CUST-00000002',
      edge_type: 'RELATED_TO',
      is_inferred: false,
      confidence: null,
    },
  ],
});
const HOUSEHOLD = envelope({
  household: { household_name: 'Lovelace Household', member_count: 2 },
  net_worth_cents: 6000000,
  total_deposits_cents: 200000,
  product_count: 4,
  member_count: 2,
});
const ENGAGEMENT = envelope({
  events: [
    {
      event_id: 'EV-1',
      event_type: 'LOGIN',
      event_date: '2026-08-01',
      channel: 'WEB',
      outcome: 'SUCCESS',
    },
  ],
});

function route(url: string): { status: number; body: unknown } | undefined {
  if (url.startsWith('/auth/token')) return { status: 200, body: TOKEN };
  if (url.startsWith('/me')) return { status: 200, body: ME };
  if (url.startsWith('/customers/CUST-00000001/360')) return { status: 200, body: VIEW_360 };
  if (url.startsWith('/customers/CUST-00000001/credit')) return { status: 200, body: CREDIT };
  if (url.startsWith('/customers/CUST-00000001/accounts')) return { status: 200, body: ACCOUNTS };
  if (url.startsWith('/customers/CUST-00000001/relationships'))
    return { status: 200, body: RELATIONSHIPS };
  if (url.startsWith('/customers/CUST-00000001/household')) return { status: 200, body: HOUSEHOLD };
  if (url.startsWith('/customers/CUST-00000001/engagement'))
    return { status: 200, body: ENGAGEMENT };
  if (url.startsWith('/customers?')) return { status: 200, body: SEARCH };
  return undefined;
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  vi.stubGlobal(
    'fetch',
    vi.fn((input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      const match = route(url);
      if (match === undefined) {
        return Promise.reject(new Error(`Unexpected request to ${url}`));
      }
      return Promise.resolve(
        new Response(JSON.stringify(match.body), {
          status: match.status,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Phase 12 app shell', () => {
  it('shows the login screen with all six seeded roles for an unauthenticated visitor', async () => {
    render(<App />);
    expect(
      await screen.findByRole('heading', { level: 1, name: /customer 360/i }),
    ).toBeInTheDocument();
    expect(screen.getByText('Relationship Manager')).toBeInTheDocument();
    expect(screen.getByText('Wealth Advisor')).toBeInTheDocument();
    expect(screen.getByText('Contact Center')).toBeInTheDocument();
    expect(screen.getByText('Branch')).toBeInTheDocument();
    expect(screen.getByText('Risk (full access)')).toBeInTheDocument();
    expect(screen.getByText('Marketing (restricted)')).toBeInTheDocument();
  });

  it('logs in, searches, opens a customer, and shows correct module states', async () => {
    const user = userEvent.setup();
    render(<App />);

    // Log in as the RM role.
    await user.click(await screen.findByRole('button', { name: /relationship manager/i }));

    // Land on search; type a query past the three-character threshold.
    const box = await screen.findByRole('combobox', { name: /find a customer/i });
    await user.type(box, 'Ada');

    // The one hit shows name, id, segment and city.
    const option = await screen.findByRole('button', { name: /ada lovelace/i });
    expect(within(option).getByText('CUST-00000001')).toBeInTheDocument();
    expect(within(option).getByText('HNW')).toBeInTheDocument();
    expect(within(option).getByText('London')).toBeInTheDocument();

    // Open the customer.
    await user.click(option);

    // The dashboard header renders the customer and role.
    expect(await screen.findByLabelText('Current customer')).toHaveTextContent('CUST-00000001');

    // The dashboard defaults to Spotlight (one section — Profile). Switch to 360° Cockpit so every
    // module is mounted and its per-module state can be asserted.
    await user.click(await screen.findByRole('button', { name: /360° Cockpit/i }));

    // Module states are distinct: contact is restricted (fields masked), risk failed (in
    // meta.errors, absent from the payload), profile is ready, and offers (absent) shows no data.
    await waitFor(() => {
      const contact = screen.getByRole('region', { name: 'Contact' });
      expect(within(contact).getByText('Restricted')).toBeInTheDocument();
    });

    const riskModule = screen.getByRole('region', { name: 'Risk analysis' });
    expect(within(riskModule).getByRole('alert')).toHaveTextContent('risk service unavailable');

    const offers = screen.getByRole('region', { name: 'Offers' });
    expect(within(offers).getByText('No data available.')).toBeInTheDocument();

    // The profile widget now renders real fields rather than a placeholder.
    const profile = screen.getByRole('region', { name: 'Profile' });
    expect(within(profile).getByText('Ada Lovelace')).toBeInTheDocument();
    expect(within(profile).getByText('CUST-00000001')).toBeInTheDocument();

    // The financial widget renders the net-worth headline and, once fetched, the FICO from /credit.
    const financial = screen.getByRole('region', { name: 'Financial overview' });
    expect(within(financial).getByText('Net worth')).toBeInTheDocument();
    await waitFor(() => {
      expect(within(financial).getByText('780')).toBeInTheDocument();
    });
  });
});
