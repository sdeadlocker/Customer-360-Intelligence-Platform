import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Envelope, Meta, Page } from '../../api/types';
import type { Signal } from './signalsApi';

// The worklist reads through the generic envelope helpers; mock them so the component test drives
// the UI deterministically without a backend (mirrors subResourceWidgets.test.tsx).
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

import { SignalsWorklist } from './SignalsWorklist';

const meta: Meta = {
  correlation_id: 'c',
  trace_id: 't',
  as_of: '2026-09-01T00:00:00Z',
  masked_fields: [],
  computed_fields: [],
  errors: [],
};

function envelope(items: Signal[]): Envelope<Page<Signal>> {
  return { data: { items, next_cursor: null }, meta };
}

function signal(overrides: Partial<Signal>): Signal {
  return {
    signal_id: 1,
    customer_id: 'C-1',
    signal_type: 'RISK_BAND_UP',
    severity: 'WARNING',
    score: 1,
    value_at_stake_cents: 1_000_000,
    evidence: { summary: 'Risk band ELEVATED', citations: [], details: {} },
    as_of: '2026-09-01',
    detected_at: '2026-09-01T00:00:00Z',
    status: 'NEW',
    ...overrides,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('SignalsWorklist', () => {
  it('renders ranked signals with severity label and glyph (not colour alone)', async () => {
    getEnvelopeMock.mockResolvedValue(
      envelope([
        signal({ signal_id: 1, severity: 'CRITICAL', signal_type: 'AML_PEP_FLAG', score: 3 }),
        signal({ signal_id: 2, severity: 'WARNING', signal_type: 'RISK_BAND_UP', score: 1 }),
      ]),
    );

    render(<SignalsWorklist onOpenCustomer={vi.fn()} />);

    const items = await screen.findAllByRole('listitem');
    expect(items).toHaveLength(2);
    // Severity is conveyed by a text label, present alongside a decorative glyph.
    expect(within(items[0]!).getByText('Critical')).toBeInTheDocument();
    expect(within(items[1]!).getByText('Warning')).toBeInTheDocument();
    // A polite status region announces the count once.
    expect(screen.getByRole('status')).toHaveTextContent('2 signals in your worklist.');
  });

  it('drills through to the customer 360 on Open', async () => {
    const onOpen = vi.fn();
    getEnvelopeMock.mockResolvedValue(envelope([signal({ customer_id: 'C-42' })]));

    render(<SignalsWorklist onOpenCustomer={onOpen} />);
    await screen.findByText('Risk band ELEVATED');
    await userEvent.click(screen.getByRole('button', { name: /open 360/i }));
    expect(onOpen).toHaveBeenCalledWith('C-42');
  });

  it('removes a signal from the queue when dismissed', async () => {
    getEnvelopeMock.mockResolvedValue(envelope([signal({ signal_id: 7 })]));
    postEnvelopeMock.mockResolvedValue({ data: { signal_id: 7, status: 'DISMISSED' }, meta });

    render(<SignalsWorklist onOpenCustomer={vi.fn()} />);
    await screen.findByText('Risk band ELEVATED');
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }));

    await waitFor(() => {
      expect(screen.queryByText('Risk band ELEVATED')).toBeNull();
    });
    expect(postEnvelopeMock).toHaveBeenCalledWith('/signals/7/dismiss', undefined);
    expect(screen.getByRole('status')).toHaveTextContent('Signal dismissed.');
  });

  it('acknowledges a signal, removing it and announcing the outcome', async () => {
    getEnvelopeMock.mockResolvedValue(envelope([signal({ signal_id: 9 })]));
    postEnvelopeMock.mockResolvedValue({ data: { signal_id: 9, status: 'ACTIONED' }, meta });

    render(<SignalsWorklist onOpenCustomer={vi.fn()} />);
    await screen.findByText('Risk band ELEVATED');
    await userEvent.click(screen.getByRole('button', { name: /acknowledge/i }));

    await waitFor(() => {
      expect(screen.queryByText('Risk band ELEVATED')).toBeNull();
    });
    expect(postEnvelopeMock).toHaveBeenCalledWith('/signals/9/ack', undefined);
  });

  it('re-queries with a type filter when a type chip is toggled', async () => {
    getEnvelopeMock.mockResolvedValue(envelope([signal({})]));

    render(<SignalsWorklist onOpenCustomer={vi.fn()} />);
    await screen.findByText('Risk band ELEVATED');

    await userEvent.click(screen.getByRole('button', { name: 'Large deposit' }));
    await waitFor(() => {
      const calls = getEnvelopeMock.mock.calls.map((c) => String(c[0]));
      expect(calls.some((url) => url.includes('type=LARGE_DEPOSIT'))).toBe(true);
    });
  });

  it('shows an empty state when there are no signals', async () => {
    getEnvelopeMock.mockResolvedValue(envelope([]));
    render(<SignalsWorklist onOpenCustomer={vi.fn()} />);
    expect(await screen.findByText(/no signals right now/i)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('No signals in your worklist.');
  });

  it('renders a banded value string verbatim without arithmetic', async () => {
    getEnvelopeMock.mockResolvedValue(envelope([signal({ value_at_stake_cents: '$1M-$10M' })]));
    render(<SignalsWorklist onOpenCustomer={vi.fn()} />);
    expect(await screen.findByText('$1M-$10M')).toBeInTheDocument();
  });
});
