import { useMemo, useState } from 'react';

import { CitationList } from './AiChrome';
import { useAskStream, type Turn } from './useAskStream';

/**
 * The ask-anything panel (task 14.5/9.6, requirements 11.1–11.9, 17.10).
 *
 * Two shapes, keyed on whether a `customerId` is supplied:
 *
 * - **Cross-customer** (`customerId` omitted): the primary entry point on the search landing page.
 *   A relationship manager can ask about any customer they are entitled to without opening a
 *   profile first — the agent searches across all customer data to resolve the question itself.
 * - **Single-customer** (a `customerId`): scoped to one customer, as embedded on their dashboard.
 *
 * Either way the answer streams in token by token, and when it completes the two citation lists
 * (facts and knowledge, kept apart per design §9.1) and any graph traversal path are shown. The
 * conversation is the session's history and is cleared on a customer switch (requirement 11.9) —
 * the {@link useAskStream} hook resets its turns and starts a fresh session id when `customerId`
 * changes.
 *
 * The three non-answer outcomes are surfaced plainly rather than dressed up as answers: a refusal
 * (a non-entitled request) shows a refusal notice disclosing nothing further (requirement 11.6); an
 * empty retrieval shows "no supporting guidance was found" (requirement 17.10); and a clarifying
 * question streams as the answer text itself. A degraded answer is badged. A transport failure on a
 * turn offers a re-ask affordance (task 14.6) by leaving the question in the composer.
 */
export function AskPanel({
  customerId,
  collapsible = false,
}: {
  readonly customerId?: string;
  /** When true, the panel starts as a slim launcher bar and expands on click (dashboard use). */
  readonly collapsible?: boolean;
}): React.JSX.Element {
  const { turns, busy, ask } = useAskStream(customerId);
  const [draft, setDraft] = useState('');
  const [expanded, setExpanded] = useState(!collapsible);
  const announcement = useAnswerAnnouncement(turns, busy);
  const crossCustomer = customerId === undefined;

  // Once a conversation exists, keep the panel open.
  const open = expanded || turns.length > 0;

  // Collapsed launcher: a slim, inviting bar that expands the full panel and focuses the input.
  if (collapsible && !open) {
    return (
      <section className="module ask-panel ask-panel--collapsed" aria-labelledby="ask-panel-title">
        <button
          type="button"
          className="ask-launcher focus-ring"
          onClick={() => {
            setExpanded(true);
          }}
        >
          <span className="ask-launcher__icon" aria-hidden="true">
            💬
          </span>
          <span className="ask-launcher__text">
            <span id="ask-panel-title" className="ask-launcher__title">
              Ask anything about this customer
            </span>
            <span className="ask-launcher__hint">
              Finances, risk, relationships, eligibility — grounded, with sources
            </span>
          </span>
          <span className="ask-launcher__cta" aria-hidden="true">
            ✨ Ask
          </span>
        </button>
      </section>
    );
  }

  const submit = (event: React.FormEvent): void => {
    event.preventDefault();
    const question = draft.trim();
    if (question === '' || busy) {
      return;
    }
    ask(question);
    setDraft('');
  };

  return (
    <section className="module ask-panel" aria-labelledby="ask-panel-title">
      <div className="module__head">
        <h2 id="ask-panel-title" className="module__title">
          Ask anything
        </h2>
        {collapsible && turns.length === 0 && (
          <button
            type="button"
            className="button button--subtle ask-panel__collapse focus-ring"
            onClick={() => {
              setExpanded(false);
            }}
          >
            Collapse
          </button>
        )}
      </div>
      <div className="module__body">
        {/* A dedicated, atomic status region announces meaningful transitions once per turn
            (asking → answer ready / refused / no guidance / failed). The streaming log itself is
            not a live region, so a screen reader is not flooded with a token-by-token readout
            (task 15.3). */}
        <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {announcement}
        </p>
        <div className="ask-panel__log">
          {turns.length === 0 && (
            <AskHero
              crossCustomer={crossCustomer}
              onPick={(question) => {
                setDraft(question);
                ask(question);
              }}
            />
          )}
          {turns.map((turn) => (
            <TurnView key={turn.id} turn={turn} onReask={() => setDraft(turn.question)} />
          ))}
        </div>

        <form className="ask-panel__composer" onSubmit={submit}>
          <label htmlFor="ask-input" className="ask-panel__label">
            Your question
          </label>
          <div className="ask-panel__row">
            <input
              id="ask-input"
              type="text"
              className="ask-panel__input"
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value);
              }}
              placeholder={
                crossCustomer
                  ? 'e.g. What are the top risks for Jane Doe?'
                  : 'e.g. Is this customer eligible for a premium credit card?'
              }
              disabled={busy}
              autoComplete="off"
            />
            <button
              type="submit"
              className="button button--primary"
              disabled={busy || draft.trim() === ''}
            >
              {busy ? 'Asking…' : 'Ask'}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}

/** Starter prompts shown as clickable chips in the hero, per surface. */
const CROSS_CUSTOMER_PROMPTS: readonly string[] = [
  'Which customers have rising risk this month?',
  'Who is eligible for a premium credit card?',
  'Show high-net-worth customers with declining balances',
];

const SINGLE_CUSTOMER_PROMPTS: readonly string[] = [
  'What are the top risks for this customer?',
  'Is this customer eligible for a premium credit card?',
  'Summarise this relationship and household',
  'What are the best next actions?',
];

/**
 * The empty-state hero for the ask panel — a branded, welcoming panel (in the spirit of the login
 * hero) shown before the first question: an AI illustration, a short lede, and clickable starter
 * prompts that fill and submit the question. It disappears as soon as a conversation begins.
 */
function AskHero({
  crossCustomer,
  onPick,
}: {
  readonly crossCustomer: boolean;
  readonly onPick: (question: string) => void;
}): React.JSX.Element {
  const prompts = crossCustomer ? CROSS_CUSTOMER_PROMPTS : SINGLE_CUSTOMER_PROMPTS;
  return (
    <div className="ask-hero">
      <div className="ask-hero__art" aria-hidden="true">
        <svg viewBox="0 0 96 96" className="ask-hero__svg" role="img">
          <defs>
            <linearGradient id="ask-hero-grad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--color-accent)" />
              <stop offset="100%" stopColor="var(--color-accent-strong)" />
            </linearGradient>
          </defs>
          {/* A chat bubble with an AI spark. */}
          <rect
            x="14"
            y="20"
            width="68"
            height="46"
            rx="14"
            fill="url(#ask-hero-grad)"
            opacity="0.16"
          />
          <rect
            x="14"
            y="20"
            width="68"
            height="46"
            rx="14"
            fill="none"
            stroke="url(#ask-hero-grad)"
            strokeWidth="2.5"
          />
          <path d="M34 66 L34 78 L48 66 Z" fill="url(#ask-hero-grad)" opacity="0.5" />
          <circle cx="34" cy="43" r="3.5" fill="url(#ask-hero-grad)" />
          <circle cx="48" cy="43" r="3.5" fill="url(#ask-hero-grad)" />
          <circle cx="62" cy="43" r="3.5" fill="url(#ask-hero-grad)" />
          <text x="72" y="24" fontSize="16" fill="var(--color-accent-strong)">
            ✦
          </text>
        </svg>
      </div>
      <h3 className="ask-hero__title">
        {crossCustomer ? 'Ask anything across your book' : 'Ask anything about this customer'}
      </h3>
      <p className="ask-hero__lede">
        {crossCustomer
          ? 'Query any customer you are entitled to — insights, risks, relationships, finances or product eligibility. Name the customer in your question.'
          : 'Ask about finances, risk, relationships, journey or product eligibility. Answers are grounded in this customer’s records, with sources you can jump to.'}
      </p>
      <div className="ask-hero__prompts">
        <span className="ask-hero__prompts-label">Try asking</span>
        <div className="ask-hero__chips">
          {prompts.map((prompt) => (
            <button
              key={prompt}
              type="button"
              className="ask-hero__chip focus-ring"
              onClick={() => {
                onPick(prompt);
              }}
            >
              <span className="ask-hero__chip-icon" aria-hidden="true">
                ✨
              </span>
              {prompt}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * Derive a single, screen-reader-friendly announcement from the latest turn (task 15.3).
 *
 * Rather than announce every streamed token, this reports the meaningful transitions: that the
 * assistant is working, and — once — the outcome (an answer is ready, the request was refused, no
 * guidance was found, or the turn failed). The message changes only when the outcome does, so the
 * polite live region fires once per turn instead of continuously.
 */
function useAnswerAnnouncement(turns: readonly Turn[], busy: boolean): string {
  const latest = turns[turns.length - 1];
  return useMemo(() => {
    if (latest === undefined) {
      return '';
    }
    if (busy && latest.status === 'streaming') {
      return 'Finding an answer…';
    }
    if (latest.status === 'error') {
      return 'The question could not be answered.';
    }
    if (latest.status === 'complete') {
      if (latest.refused) {
        return 'Request refused.';
      }
      if (latest.noGuidance) {
        return 'No guidance found.';
      }
      return 'Answer ready.';
    }
    return '';
  }, [latest, busy]);
}

function TurnView({
  turn,
  onReask,
}: {
  readonly turn: Turn;
  readonly onReask: () => void;
}): React.JSX.Element {
  return (
    <div className="qa-turn">
      <p className="qa-turn__question">
        <span className="qa-turn__role">You</span> {turn.question}
      </p>

      <div className="qa-turn__answer">
        <span className="qa-turn__role">Assistant</span>
        {turn.status === 'error' ? (
          <div role="alert" className="ai-card__failure">
            <p>{turn.errorMessage ?? 'The question could not be answered.'}</p>
            <button type="button" className="button" onClick={onReask}>
              Retry
            </button>
          </div>
        ) : (
          <TurnBody turn={turn} />
        )}
      </div>
    </div>
  );
}

function TurnBody({ turn }: { readonly turn: Turn }): React.JSX.Element {
  const streaming = turn.status === 'streaming';

  if (turn.refused) {
    return (
      <p className="qa-turn__notice qa-turn__notice--refused" role="status">
        {turn.answer !== '' ? turn.answer : 'This request cannot be answered for your role.'}
      </p>
    );
  }

  if (turn.status === 'complete' && turn.noGuidance) {
    return (
      <p className="qa-turn__notice qa-turn__notice--no-guidance" role="status">
        {turn.answer !== '' ? turn.answer : 'No supporting guidance was found.'}
      </p>
    );
  }

  return (
    <>
      <p className="qa-turn__text">
        {turn.answer}
        {streaming && <span className="ai-cursor" aria-hidden="true" />}
      </p>

      {turn.degraded && (
        <span
          className="badge badge--degraded"
          title="The AI model was unavailable; this is a deterministic fallback."
        >
          Degraded
        </span>
      )}

      {turn.traversalPaths.length > 0 && (
        <div className="qa-turn__paths">
          <span className="citations__label">Traversal path</span>
          {turn.traversalPaths.map((path, i) => (
            <p key={i} className="qa-turn__path mono">
              {path.join(' → ')}
            </p>
          ))}
        </div>
      )}

      {turn.status === 'complete' && (
        <CitationList facts={turn.factCitations} passages={turn.passageCitations} />
      )}
    </>
  );
}
