import { AiCard } from './AiCard';
import {
  FinancialHealthCardBody,
  JourneyCardBody,
  OfferCardBody,
  RelationshipCardBody,
  RiskCardBody,
  SummaryCardBody,
} from './cards';
import { useAgentStream } from './useAgentStream';

/**
 * The AI experience layer for one customer's dashboard (Phase 14).
 *
 * It opens two SSE streams — `/insights` (every agent except the offer recommendation) and
 * `/recommendations` (the offer agent) — and renders each agent's card from the stream, so a fast
 * card (financial health) appears without waiting on the slow summary (design §8.1). Each card
 * wears the shared {@link ./AiCard.AiCard} shell, which owns the AI-generated label, timestamp,
 * cache and degraded badges, the citation lists, the "generating…" pending state and the per-card
 * retry affordance (tasks 14.1, 14.2, 14.4, 14.6).
 *
 * The ask-anything panel ({@link ./AskPanel.AskPanel}) is not part of this card stack: the
 * cross-customer entry point lives on the search landing page (task 9.6), and the dashboard mounts
 * a single-customer AskPanel as its own "Ask AI" section (scoped to the customer in view) rather
 * than inside this insights card grid.
 *
 * The two streams are independent, so a failure to open one (the recommendations stream) never
 * blanks the other's cards; each card's `retry` re-opens its own stream.
 */
export function AiExperience({ customerId }: { readonly customerId: string }): React.JSX.Element {
  const insights = useAgentStream(`/customers/${encodeURIComponent(customerId)}/insights`, {
    markFirstCard: true,
  });
  const recommendations = useAgentStream(
    `/customers/${encodeURIComponent(customerId)}/recommendations`,
  );

  return (
    <section className="ai-experience" aria-label="AI insights and recommendations">
      <AiCard
        title="AI summary"
        card={insights.cards.get('customer_summary')}
        status={insights.status}
        error={insights.error}
        onRetry={insights.retry}
      >
        {(card) => <SummaryCardBody card={card} />}
      </AiCard>

      <AiCard
        title="Next best actions"
        card={recommendations.cards.get('offer_recommendation')}
        status={recommendations.status}
        error={recommendations.error}
        onRetry={recommendations.retry}
      >
        {(card) => <OfferCardBody card={card} />}
      </AiCard>

      <AiCard
        title="Financial health (AI)"
        card={insights.cards.get('financial_health')}
        status={insights.status}
        error={insights.error}
        onRetry={insights.retry}
      >
        {(card) => <FinancialHealthCardBody card={card} />}
      </AiCard>

      <AiCard
        title="Risk (AI)"
        card={insights.cards.get('risk')}
        status={insights.status}
        error={insights.error}
        onRetry={insights.retry}
      >
        {(card) => <RiskCardBody card={card} />}
      </AiCard>

      <AiCard
        title="Relationships (AI)"
        card={insights.cards.get('relationship')}
        status={insights.status}
        error={insights.error}
        onRetry={insights.retry}
      >
        {(card) => <RelationshipCardBody card={card} />}
      </AiCard>

      <AiCard
        title="Journey (AI)"
        card={insights.cards.get('journey')}
        status={insights.status}
        error={insights.error}
        onRetry={insights.retry}
      >
        {(card) => <JourneyCardBody card={card} />}
      </AiCard>
    </section>
  );
}
