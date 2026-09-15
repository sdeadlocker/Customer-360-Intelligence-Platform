import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type * as SseModule from '../../api/sse';
import type { SseEvent, SseRequest } from '../../api/sse';

/**
 * Tests for the Phase 14 AI experience layer.
 *
 * The streams are driven through a mocked `openSse`, so each test scripts the exact SSE frames the
 * backend would emit and asserts the UI behaviour the tasks require: honest AI labelling and the
 * degraded badge (14.1), the two distinct citation types and the passage viewer (14.2), the ranked
 * next-best-actions with cited eligibility (14.3), the narrative cards including risk next steps
 * (14.4), the ask panel's separated citations, traversal path, refusal and no-guidance handling and
 * clearing on customer switch (14.5), and the per-card retry affordance on failure (14.6).
 */

// A programmable queue of scripted streams, one shift() per `openSse` call, in call order.
const streamQueue: (SseEvent[] | { error: { status: number } })[] = [];
// The requests `openSse` was called with, in order, so a test can assert the path and body.
const sseRequests: SseRequest[] = [];

vi.mock('../../api/sse', async () => {
  const actual = await vi.importActual<typeof SseModule>('../../api/sse');
  return {
    ...actual,
    openSse: (request: SseRequest): AsyncGenerator<SseEvent, void, void> => {
      sseRequests.push(request);
      const scripted = streamQueue.shift() ?? [];
      async function* gen(): AsyncGenerator<SseEvent, void, void> {
        // A trivial await keeps this a genuine async generator (and satisfies require-await).
        await Promise.resolve();
        if (!Array.isArray(scripted)) {
          throw new actual.SseError('stream failed', scripted.error.status);
        }
        for (const frame of scripted) {
          yield frame;
        }
      }
      return gen();
    },
  };
});

// RUM marks are a no-op side effect in tests; stub so they never touch the network.
vi.mock('../../rum/rum', () => ({ mark: vi.fn() }));

import { AiExperience } from './AiExperience';
import { AskPanel } from './AskPanel';
import { CitationProvider } from './CitationContext';
import { PassageViewer } from './PassageViewer';

beforeEach(() => {
  streamQueue.length = 0;
  sseRequests.length = 0;
});

afterEach(() => {
  vi.restoreAllMocks();
});

function agentFrame(agent: string, outputs: Record<string, unknown>, overrides = {}): SseEvent {
  return {
    event: 'agent',
    data: {
      agent,
      outputs,
      fact_citations: [],
      passage_citations: [],
      unavailable_inputs: [],
      confidence: null,
      generated_at: '2026-09-14T10:00:00Z',
      model_id: 'mock',
      prompt_version: 'v1',
      cache_hit: false,
      degraded: false,
      ...overrides,
    },
  };
}

function renderExperience(): void {
  render(
    <CitationProvider>
      <AiExperience customerId="C1" />
      <PassageViewer />
    </CitationProvider>,
  );
}

describe('AI summary card (14.1)', () => {
  it('renders the executive summary, snapshot chips and the AI-generated label', async () => {
    // insights stream, then recommendations stream.
    streamQueue.push([
      agentFrame(
        'customer_summary',
        {
          narrative: '',
          executive_summary: 'A mass-market saver with steady inflows.',
          snapshot: [{ label: 'Net worth $120K', fact_id: 'F1' }],
          advisor_notes: [{ note: 'Discuss an emergency-fund top-up.' }],
        },
        {
          fact_citations: [
            {
              kind: 'fact',
              fact_id: 'F1',
              entity_type: 'financial_profile',
              entity_id: 'C1',
              field: 'net_worth_cents',
            },
          ],
        },
      ),
      { event: 'done', data: { agents: ['customer_summary'] } },
    ]);
    streamQueue.push([{ event: 'done', data: { agents: [] } }]);

    renderExperience();

    expect(await screen.findByText('A mass-market saver with steady inflows.')).toBeInTheDocument();
    expect(screen.getByText('Net worth $120K')).toBeInTheDocument();
    expect(screen.getByText('Discuss an emergency-fund top-up.')).toBeInTheDocument();
    expect(screen.getAllByText('AI-generated').length).toBeGreaterThan(0);
  });

  it('shows the degraded badge when a card fell back to a template', async () => {
    streamQueue.push([
      agentFrame(
        'customer_summary',
        { narrative: 'Fallback summary.', executive_summary: '', snapshot: [], advisor_notes: [] },
        { degraded: true },
      ),
      { event: 'done', data: { agents: ['customer_summary'] } },
    ]);
    streamQueue.push([{ event: 'done', data: { agents: [] } }]);

    renderExperience();

    expect(await screen.findByText('Fallback summary.')).toBeInTheDocument();
    expect(screen.getByText('Degraded')).toBeInTheDocument();
  });
});

describe('citation inspection (14.2)', () => {
  it('opens the passage viewer with document metadata for a knowledge citation', async () => {
    streamQueue.push([
      agentFrame(
        'risk',
        {
          narrative: 'Elevated risk.',
          risk_band: 'ELEVATED',
          drivers: [],
          alerts: [],
          compliance_flag: false,
          next_steps: [],
        },
        {
          passage_citations: [
            {
              kind: 'passage',
              passage_id: 'P1',
              doc_id: 'policy-101',
              section_path: 'Risk / Escalation',
              title: 'Risk Escalation Policy',
              version: '2',
              effective_from: '2025-01-01',
              effective_to: null,
            },
          ],
        },
      ),
      { event: 'done', data: { agents: ['risk'] } },
    ]);
    streamQueue.push([{ event: 'done', data: { agents: [] } }]);

    renderExperience();

    const chip = await screen.findByRole('button', { name: /Risk Escalation Policy/ });
    await userEvent.click(chip);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Risk Escalation Policy')).toBeInTheDocument();
    expect(within(dialog).getByText('Risk / Escalation')).toBeInTheDocument();
    expect(within(dialog).getByText('policy-101')).toBeInTheDocument();
  });
});

describe('next best actions (14.3)', () => {
  it('ranks offers with rationale and shows cited eligibility notes', async () => {
    streamQueue.push([{ event: 'done', data: { agents: [] } }]); // insights
    streamQueue.push([
      agentFrame('offer_recommendation', {
        narrative: 'Two strong offers.',
        ranked_offers: [
          {
            offer_id: 'OFF-1',
            rationale: 'High affinity for cards.',
            suppressed: false,
            suppression_reason: null,
          },
          {
            offer_id: 'OFF-2',
            rationale: 'Already holds product.',
            suppressed: true,
            suppression_reason: 'duplicate product',
          },
        ],
        eligibility_notes: ['Minimum FICO 700 for premium card.'],
      }),
      { event: 'done', data: { agents: ['offer_recommendation'] } },
    ]);

    renderExperience();

    expect(await screen.findByText('High affinity for cards.')).toBeInTheDocument();
    expect(screen.getByText('Suppressed')).toBeInTheDocument();
    expect(screen.getByText(/duplicate product/)).toBeInTheDocument();
    expect(screen.getByText('Minimum FICO 700 for premium card.')).toBeInTheDocument();
  });
});

describe('risk narrative card (14.4)', () => {
  it('renders prescribed next steps and the compliance banner', async () => {
    streamQueue.push([
      agentFrame('risk', {
        narrative: 'Watch this account.',
        risk_band: 'HIGH',
        drivers: [{ factor: 'Delinquency', severity: 'high' }],
        alerts: ['30 days past due'],
        compliance_flag: true,
        next_steps: ['Escalate to the risk desk.'],
      }),
      { event: 'done', data: { agents: ['risk'] } },
    ]);
    streamQueue.push([{ event: 'done', data: { agents: [] } }]);

    renderExperience();

    expect(await screen.findByText('Escalate to the risk desk.')).toBeInTheDocument();
    expect(screen.getByText(/Compliance review required/)).toBeInTheDocument();
  });
});

describe('per-card retry and failure (14.6)', () => {
  it('shows a retry affordance when the stream fails to open', async () => {
    streamQueue.push({ error: { status: 503 } }); // insights fails
    streamQueue.push([{ event: 'done', data: { agents: [] } }]); // recommendations

    renderExperience();

    // The insights cards share the failed stream; a retry button is offered.
    const retries = await screen.findAllByRole('button', { name: 'Retry' });
    expect(retries.length).toBeGreaterThan(0);
    expect(screen.getAllByText(/AI layer is currently unavailable/).length).toBeGreaterThan(0);
  });
});

describe('ask-anything panel (14.5)', () => {
  it('streams an answer and shows separated fact and knowledge citations with a traversal path', async () => {
    streamQueue.push([
      { event: 'token', data: { text: 'The household ' } },
      { event: 'token', data: { text: 'has three members.' } },
      {
        event: 'citations',
        data: {
          fact_citations: [
            {
              kind: 'fact',
              fact_id: 'F1',
              entity_type: 'household',
              entity_id: 'H1',
              field: 'member_count',
            },
          ],
          passage_citations: [
            {
              kind: 'passage',
              passage_id: 'P1',
              doc_id: 'kb-1',
              section_path: 'Households',
              title: 'Household Guide',
              version: '1',
              effective_from: '2025-01-01',
              effective_to: null,
            },
          ],
          traversal_paths: [['C1', 'H1', 'C2']],
        },
      },
      {
        event: 'done',
        data: {
          refused: false,
          no_guidance: false,
          degraded: false,
          route: 'both',
          model_id: 'mock',
        },
      },
    ]);

    render(
      <CitationProvider>
        <AskPanel customerId="C1" />
        <PassageViewer />
      </CitationProvider>,
    );

    await userEvent.type(screen.getByLabelText('Your question'), 'How big is the household?');
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }));

    expect(await screen.findByText('The household has three members.')).toBeInTheDocument();
    // Facts and knowledge are labelled separately.
    expect(screen.getByText('Facts')).toBeInTheDocument();
    expect(screen.getByText('Knowledge')).toBeInTheDocument();
    // The traversal path is displayed.
    expect(screen.getByText('C1 → H1 → C2')).toBeInTheDocument();
  });

  it('surfaces a refusal plainly without disclosing anything', async () => {
    streamQueue.push([
      { event: 'token', data: { text: 'I cannot answer that.' } },
      {
        event: 'citations',
        data: { fact_citations: [], passage_citations: [], traversal_paths: [] },
      },
      {
        event: 'done',
        data: { refused: true, no_guidance: false, degraded: false, route: '', model_id: 'mock' },
      },
    ]);

    render(
      <CitationProvider>
        <AskPanel customerId="C1" />
      </CitationProvider>,
    );

    await userEvent.type(screen.getByLabelText('Your question'), 'Show me another customer.');
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }));

    expect(await screen.findByText('I cannot answer that.')).toBeInTheDocument();
    // No citation lists are shown for a refusal.
    expect(screen.queryByText('Facts')).not.toBeInTheDocument();
  });

  it('shows a no-guidance notice when retrieval was empty', async () => {
    streamQueue.push([
      { event: 'token', data: { text: '' } },
      {
        event: 'citations',
        data: { fact_citations: [], passage_citations: [], traversal_paths: [] },
      },
      {
        event: 'done',
        data: {
          refused: false,
          no_guidance: true,
          degraded: false,
          route: 'knowledge',
          model_id: 'mock',
        },
      },
    ]);

    render(
      <CitationProvider>
        <AskPanel customerId="C1" />
      </CitationProvider>,
    );

    await userEvent.type(screen.getByLabelText('Your question'), 'What is the policy on unicorns?');
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }));

    expect(await screen.findByText('No supporting guidance was found.')).toBeInTheDocument();
  });

  it('clears the conversation when the customer changes (no cross-customer leakage)', async () => {
    streamQueue.push([
      { event: 'token', data: { text: 'Answer for C1.' } },
      {
        event: 'citations',
        data: { fact_citations: [], passage_citations: [], traversal_paths: [] },
      },
      {
        event: 'done',
        data: {
          refused: false,
          no_guidance: false,
          degraded: false,
          route: 'facts',
          model_id: 'mock',
        },
      },
    ]);

    const { rerender } = render(
      <CitationProvider>
        <AskPanel customerId="C1" />
      </CitationProvider>,
    );

    await userEvent.type(screen.getByLabelText('Your question'), 'Question one?');
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await screen.findByText('Answer for C1.')).toBeInTheDocument();

    // Switch customer: the prior turn must be gone.
    rerender(
      <CitationProvider>
        <AskPanel customerId="C2" />
      </CitationProvider>,
    );
    await waitFor(() => {
      expect(screen.queryByText('Answer for C1.')).not.toBeInTheDocument();
    });
  });

  it('posts to the cross-customer /ask endpoint when no customer is selected (9.6)', async () => {
    streamQueue.push([
      { event: 'token', data: { text: 'Jane Doe is a mass-market saver.' } },
      {
        event: 'citations',
        data: { fact_citations: [], passage_citations: [], traversal_paths: [] },
      },
      {
        event: 'done',
        data: {
          refused: false,
          no_guidance: false,
          degraded: false,
          route: 'facts',
          model_id: 'mock',
        },
      },
    ]);

    // No customerId: the search-landing "Ask anything" surface, which searches across all customers.
    render(
      <CitationProvider>
        <AskPanel />
      </CitationProvider>,
    );

    await userEvent.type(screen.getByLabelText('Your question'), 'Who is Jane Doe?');
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }));

    expect(await screen.findByText('Jane Doe is a mass-market saver.')).toBeInTheDocument();
    // The request went to the cross-customer endpoint, not a customer-scoped one.
    expect(sseRequests).toHaveLength(1);
    expect(sseRequests[0]?.path).toBe('/ask');
  });
});
