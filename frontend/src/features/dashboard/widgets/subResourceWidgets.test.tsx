import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Envelope, Meta } from '../../../api/types';
import { EngagementWidget } from './EngagementWidget';
import { RelationshipWidget } from './RelationshipWidget';

/**
 * Tests for the widgets that fetch a sub-resource outside the composed 360 payload. The API client's
 * `getEnvelope` is stubbed per path so the widget's own fetch, restricted-node handling and filters
 * can be exercised in isolation.
 */

const { getEnvelopeMock } = vi.hoisted(() => ({ getEnvelopeMock: vi.fn() }));

vi.mock('../../../api/client', () => ({
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

beforeEach(() => {
  getEnvelopeMock.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('RelationshipWidget', () => {
  it('renders a restricted node as structure-only with no identity', async () => {
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
                node_id: 'CUSTOMER:restricted-abc',
                node_type: 'Customer',
                entity_id: '',
                label: 'Restricted',
                props: { restricted: true },
              },
            ],
            edges: [
              {
                src_id: 'CUSTOMER:C1',
                dst_id: 'CUSTOMER:restricted-abc',
                edge_type: 'RELATED_TO',
                is_inferred: false,
                confidence: null,
              },
            ],
          }),
        );
      }
      return Promise.resolve(envelope({ household: null }));
    });

    render(
      <MemoryRouter>
        <RelationshipWidget customerId="C1" />
      </MemoryRouter>,
    );

    // The restricted node is selectable and its inspector discloses nothing but structure.
    const restricted = await screen.findByRole('button', {
      name: /restricted customer \(structure only\)/i,
    });
    await userEvent.click(restricted);
    const inspector = screen.getByRole('group', { name: /node detail/i });
    expect(within(inspector).getByText(/identity is withheld/i)).toBeInTheDocument();
    // No "Open 360 view" navigation is offered for a restricted node.
    expect(within(inspector).queryByRole('button', { name: /open 360 view/i })).toBeNull();
  });

  it('offers navigation to an entitled neighbouring customer', async () => {
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
            ],
            edges: [],
          }),
        );
      }
      return Promise.resolve(envelope({ household: null }));
    });

    render(
      <MemoryRouter>
        <RelationshipWidget customerId="C1" />
      </MemoryRouter>,
    );

    const neighbour = await screen.findByRole('button', { name: /Customer: Neighbour/i });
    await userEvent.click(neighbour);
    expect(screen.getByRole('button', { name: /open 360 view/i })).toBeInTheDocument();
  });
});

describe('EngagementWidget', () => {
  it('lists events and filters by channel', async () => {
    getEnvelopeMock.mockResolvedValue(
      envelope({
        events: [
          {
            event_id: 'E1',
            event_type: 'LOGIN',
            event_date: '2026-08-01',
            channel: 'WEB',
            outcome: 'SUCCESS',
          },
          {
            event_id: 'E2',
            event_type: 'BRANCH_VISIT',
            event_date: '2026-07-01',
            channel: 'BRANCH',
            outcome: null,
          },
        ],
      }),
    );

    render(<EngagementWidget customerId="C1" />);

    await waitFor(() => {
      expect(screen.getByRole('table', { name: /engagement events/i })).toBeInTheDocument();
    });
    const table = () => screen.getByRole('table', { name: /engagement events/i });
    expect(within(table()).getByText('Login')).toBeInTheDocument();
    expect(within(table()).getByText('Branch visit')).toBeInTheDocument();

    // Filter to WEB only; the branch event drops out of the table body.
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /channel/i }), 'WEB');
    expect(within(table()).queryByText('Branch visit')).toBeNull();
    expect(within(table()).getByText('Login')).toBeInTheDocument();
  });
});
