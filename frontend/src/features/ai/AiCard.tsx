import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

import type { AgentCard } from './types';
import type { StreamStatus } from './useAgentStream';
import { AiMeta, CitationList } from './AiChrome';

/**
 * The shell every dashboard AI card renders inside (tasks 14.1, 14.3, 14.4, 14.6).
 *
 * A card has one of four honest states, and this shell renders them so no card ever misleads:
 *
 * - **awaiting** — the stream is open but this agent's frame has not arrived yet: a "generating…"
 *   placeholder, never an empty card presented as complete.
 * - **ready** — the card arrived: its content, the AI-generated label with timestamp and cache
 *   indicator, the two citation lists, and — if the model fell back — the degraded badge (task
 *   14.1). A degraded card is shown, but honestly badged; a partial narrative is never dressed up
 *   as a finished one (task 14.6).
 * - **failed** — the whole stream failed to open (a 404/503/transport error). The card offers a
 *   retry affordance rather than a blank space (task 14.6).
 * - **absent** — the stream finished and this agent produced nothing (an isolated customer, an
 *   unentitled role): a plain "not available" line, distinct from a failure.
 *
 * `title` names the card; `children` is the agent-specific body a caller renders from the card's
 * typed `outputs`. The shell owns the chrome so each card component stays focused on its own shape.
 */

export interface AiCardProps {
  readonly title: string;
  readonly card: AgentCard | undefined;
  readonly status: StreamStatus;
  readonly error: string | null;
  readonly onRetry: () => void;
  /** The agent-specific body, rendered only when the card is present. */
  readonly children: (card: AgentCard) => ReactNode;
  /** Message for the "stream finished, no card" case. */
  readonly absentLabel?: string;
}

export function AiCard({
  title,
  card,
  status,
  error,
  onRetry,
  children,
  absentLabel = 'Not available for this customer.',
}: AiCardProps): React.JSX.Element {
  return (
    <section className="module ai-card" aria-labelledby={`ai-${slug(title)}-title`}>
      <div className="module__head">
        <h2 id={`ai-${slug(title)}-title`} className="module__title">
          {title}
        </h2>
      </div>
      <div className="module__body" aria-live="polite" aria-busy={status === 'streaming' && !card}>
        <AiCardBody
          card={card}
          status={status}
          error={error}
          onRetry={onRetry}
          absentLabel={absentLabel}
        >
          {children}
        </AiCardBody>
      </div>
    </section>
  );
}

function AiCardBody({
  card,
  status,
  error,
  onRetry,
  children,
  absentLabel,
}: Omit<AiCardProps, 'title'>): React.JSX.Element {
  if (card !== undefined) {
    return (
      <div className="ai-card__content">
        <AiMeta card={card} />
        {/* The agent body is clamped to a uniform height so the AI cards (summary, financial health,
            risk, next best actions) line up at the same size; a "See more" reveals the rest. */}
        <CollapsibleBody>{children(card)}</CollapsibleBody>
        <CitationList
          facts={card.fact_citations}
          passages={card.passage_citations}
          unavailableInputs={card.unavailable_inputs}
        />
      </div>
    );
  }

  if (status === 'error') {
    return <CardFailure message={error ?? 'The AI card could not be loaded.'} onRetry={onRetry} />;
  }

  if (status === 'streaming') {
    return (
      <p role="status" className="ai-card__pending">
        <span className="ai-spinner" aria-hidden="true" /> Generating…
      </p>
    );
  }

  // status === 'done' but no card for this agent.
  return <p className="module__empty">{absentLabel ?? 'Not available for this customer.'}</p>;
}

/**
 * Clamp an AI card's body to a uniform height with a "See more" / "See less" toggle, so the AI
 * cards align at the same size regardless of narrative length. The toggle appears only when the
 * content actually exceeds the clamp (measured after layout), so short cards are unaffected.
 */
function CollapsibleBody({ children }: { readonly children: ReactNode }): React.JSX.Element {
  const ref = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el === null) {
      return;
    }
    // scrollHeight exceeds clientHeight when the clamped body has hidden content.
    setOverflowing(el.scrollHeight - el.clientHeight > 4);
  }, [children]);

  return (
    <div className="ai-card__collapse">
      <div ref={ref} className={`ai-card__clamp${expanded ? ' ai-card__clamp--open' : ''}`}>
        {children}
      </div>
      {(overflowing || expanded) && (
        <button
          type="button"
          className="button button--subtle ai-card__seemore focus-ring"
          aria-expanded={expanded}
          onClick={() => {
            setExpanded((v) => !v);
          }}
        >
          {expanded ? 'See less' : 'See more'}
        </button>
      )}
    </div>
  );
}

/** The failure affordance shared by every AI surface (task 14.6). */
export function CardFailure({
  message,
  onRetry,
}: {
  readonly message: string;
  readonly onRetry: () => void;
}): React.JSX.Element {
  return (
    <div role="alert" className="ai-card__failure">
      <p>{message}</p>
      <button type="button" className="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');
}
