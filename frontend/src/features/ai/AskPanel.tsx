import { useEffect, useMemo, useRef, useState } from 'react';

import { CitationList } from './AiChrome';
import { useAskStream, type Turn } from './useAskStream';
import { AssistantAvatar, type AvatarState } from './voice/AssistantAvatar';
import { useSpeechInput } from './voice/useSpeechInput';
import { useSpeechOutput } from './voice/useSpeechOutput';

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
  const { turns, busy, ask, reset } = useAskStream(customerId);
  const [draft, setDraft] = useState('');
  const [expanded, setExpanded] = useState(!collapsible);
  const announcement = useAnswerAnnouncement(turns, busy);
  const crossCustomer = customerId === undefined;

  // The scrolling conversation log. As turns are added and the answer streams in, keep the newest
  // content in view so the user reads the latest message rather than being stranded at the top.
  const logRef = useRef<HTMLDivElement | null>(null);
  const lastTurn = turns[turns.length - 1];
  useEffect(() => {
    const log = logRef.current;
    if (log !== null) {
      log.scrollTop = log.scrollHeight;
    }
  }, [turns.length, lastTurn?.answer, busy]);

  // The voice layer (Phase 23). Both are feature-detected: on a browser without the API the hook
  // reports `supported: false` and the affordance is simply not rendered.
  const voiceIn = useSpeechInput();
  const voiceOut = useSpeechOutput();

  // Collapse the panel back to its launcher: clear the conversation and any in-flight stream, and
  // return to the slim closed state. Only meaningful when the panel is collapsible.
  const collapse = (): void => {
    voiceIn.stop();
    voiceOut.cancel();
    reset();
    setExpanded(false);
  };

  // Speak each answer once, when it settles — never mid-stream (which would stutter), and only the
  // newest turn (so re-renders do not re-speak history). Tracked by turn id.
  const spokenTurnRef = useRef<number | null>(null);
  const latest = turns[turns.length - 1];
  useEffect(() => {
    // Speak only when output is on. The "already spoken" ref is set only after a real utterance, so
    // toggling voice on *after* an answer has landed still speaks that answer (the ref was never
    // marked while output was off).
    if (
      voiceOut.enabled &&
      latest?.status === 'complete' &&
      !latest.refused &&
      latest.answer !== '' &&
      spokenTurnRef.current !== latest.id
    ) {
      spokenTurnRef.current = latest.id;
      voiceOut.speak(latest.answer);
    }
  }, [latest, voiceOut]);

  // The avatar's state is the conversation's state: listening to the mic wins, then a streaming
  // answer is "thinking", then a spoken reply is "speaking", else at rest.
  const avatarState: AvatarState =
    voiceIn.state === 'listening'
      ? 'listening'
      : busy
        ? 'thinking'
        : voiceOut.state === 'speaking'
          ? 'speaking'
          : 'idle';

  const startDictation = (): void => {
    // Speaking and listening at once would feed the reply back into the mic, so silence output first.
    voiceOut.cancel();
    voiceIn.start((text) => {
      setDraft(text);
    });
  };

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
              {crossCustomer ? 'Ask anything across your book' : 'Ask anything about this customer'}
            </span>
            <span className="ask-launcher__hint">
              {crossCustomer
                ? 'Any customer you are entitled to — insights, risks, finances, eligibility'
                : 'Finances, risk, relationships, eligibility — grounded, with sources'}
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
      <div className="module__head ask-panel__head">
        <h2 id="ask-panel-title" className="module__title">
          Ask anything
        </h2>
        <div className="ask-panel__head-actions">
          {voiceOut.supported && (
            <button
              type="button"
              className={`ask-voice-toggle focus-ring${voiceOut.enabled ? ' ask-voice-toggle--on' : ''}`}
              aria-pressed={voiceOut.enabled}
              onClick={() => {
                voiceOut.setEnabled(!voiceOut.enabled);
              }}
              title={voiceOut.enabled ? 'Spoken replies on' : 'Spoken replies off'}
            >
              <span aria-hidden="true">{voiceOut.enabled ? '🔊' : '🔈'}</span>
              <span className="ask-voice-toggle__label">
                {voiceOut.enabled ? 'Voice on' : 'Voice off'}
              </span>
            </button>
          )}
          {collapsible && (
            <button
              type="button"
              className="button button--subtle ask-panel__collapse focus-ring"
              onClick={collapse}
              title={turns.length > 0 ? 'Close this conversation' : 'Collapse'}
            >
              Collapse
            </button>
          )}
        </div>
      </div>
      <div className="module__body">
        {/* A dedicated, atomic status region announces meaningful transitions once per turn
            (asking → answer ready / refused / no guidance / failed). The streaming log itself is
            not a live region, so a screen reader is not flooded with a token-by-token readout
            (task 15.3). */}
        <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {announcement}
        </p>

        {/* Two columns: a portrait rail on the left (like a chat contact), the conversation on the
            right. The rail is only shown when the browser supports speech — it is the voice
            assistant's face — and it collapses under the chat on a narrow panel via CSS. */}
        <div className={`ask-layout${voiceOut.supported ? '' : ' ask-layout--no-rail'}`}>
          {voiceOut.supported && (
            <aside className="ask-rail">
              <AssistantAvatar state={avatarState} mouth={voiceOut.mouth} size={148} />
              <p className="ask-rail__name">
                Ava <span className="ask-rail__role">· AI assistant</span>
              </p>
              <p className={`ask-rail__status ask-rail__status--${avatarState}`}>
                <span className="ask-rail__status-dot" aria-hidden="true" />
                {railStatus(avatarState)}
              </p>
              {voiceIn.supported && (
                <button
                  type="button"
                  className={`ask-rail__speak focus-ring${
                    voiceIn.state === 'listening' ? ' ask-rail__speak--live' : ''
                  }`}
                  aria-pressed={voiceIn.state === 'listening'}
                  aria-label={
                    voiceIn.state === 'listening' ? 'Stop dictating' : 'Speak your question'
                  }
                  onClick={() => {
                    if (voiceIn.state === 'listening') {
                      voiceIn.stop();
                    } else {
                      startDictation();
                    }
                  }}
                >
                  <span aria-hidden="true">🎙</span>
                  {voiceIn.state === 'listening' ? 'Listening…' : 'Speak'}
                </button>
              )}
            </aside>
          )}

          <div className="ask-chat">
            <div className="ask-panel__log" ref={logRef}>
              {turns.length === 0 && (
                <AskHero
                  crossCustomer={crossCustomer}
                  onPick={(question) => {
                    ask(question);
                    setDraft('');
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
                    voiceIn.state === 'listening'
                      ? 'Listening…'
                      : crossCustomer
                        ? 'e.g. What are the top risks for Jane Doe?'
                        : 'e.g. Is this customer eligible for a premium credit card?'
                  }
                  disabled={busy}
                  autoComplete="off"
                />
                {voiceIn.supported && (
                  <button
                    type="button"
                    className={`ask-mic focus-ring${voiceIn.state === 'listening' ? ' ask-mic--live' : ''}`}
                    aria-pressed={voiceIn.state === 'listening'}
                    aria-label={voiceIn.state === 'listening' ? 'Stop dictating' : 'Ask by voice'}
                    disabled={busy}
                    onClick={() => {
                      if (voiceIn.state === 'listening') {
                        voiceIn.stop();
                      } else {
                        startDictation();
                      }
                    }}
                  >
                    <span aria-hidden="true">🎤</span>
                  </button>
                )}
                <button
                  type="submit"
                  className="button button--primary"
                  disabled={busy || draft.trim() === ''}
                >
                  {busy ? 'Asking…' : 'Ask'}
                </button>
              </div>

              {voiceIn.state === 'listening' && (
                <p className="ask-voice-hint" role="status">
                  <span className="ask-voice-hint__dot" aria-hidden="true" /> Listening — speak your
                  question, then pause.
                </p>
              )}
              {voiceIn.error !== null && (
                <p className="ask-voice-error" role="alert">
                  {voiceIn.error}
                </p>
              )}
              {/* Stated once, where the mic lives: browser dictation is not on-device. */}
              {voiceIn.supported && voiceIn.state !== 'listening' && voiceIn.error === null && (
                <p className="ask-voice-note">
                  Voice input is transcribed by your browser, which may send audio to its provider.
                </p>
              )}
            </form>
          </div>
        </div>
      </div>
    </section>
  );
}

/** The one-word live status under the assistant's name in the rail, like a chat contact. */
function railStatus(state: AvatarState): string {
  switch (state) {
    case 'listening':
      return 'Listening…';
    case 'thinking':
      return 'Thinking…';
    case 'speaking':
      return 'Speaking…';
    default:
      return 'Online';
  }
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

/**
 * Render answer text with the light markdown a model emits — `**bold**` becomes real bold rather
 * than showing literal asterisks. Line breaks are preserved by the `white-space: pre-wrap` on
 * `.qa-turn__text`, so only inline emphasis needs handling here. Done by splitting on the `**`
 * delimiter (no HTML injection): even-index segments are plain text, odd-index segments are bold.
 */
function FormattedAnswer({ text }: { readonly text: string }): React.JSX.Element {
  const parts = text.split(/\*\*/);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? <strong key={index}>{part}</strong> : <span key={index}>{part}</span>,
      )}
    </>
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
        <FormattedAnswer text={turn.answer} />
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
