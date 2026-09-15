import { useCallback, useEffect, useRef, useState } from 'react';

import { openSse, SseError } from '../../api/sse';
import { mark } from '../../rum/rum';
import type { AgentCard } from './types';

/**
 * Subscribe to a dashboard agent SSE stream (`/insights` or `/recommendations`) and expose each
 * card as it arrives (tasks 14.1, 14.3, 14.4; design §8.1).
 *
 * The stream delivers one `agent` frame per card and a terminal `done` frame. This hook collects
 * the cards into a map keyed by agent name so a component can pick out just its own card, and tracks
 * a stream-level status so the UI can tell "still streaming", "finished" and "the stream itself
 * failed to open" apart. A per-card failure is *not* a stream failure: a degraded card arrives as a
 * normal `agent` frame with `degraded: true`, and the card component renders it honestly (task
 * 14.6) rather than the whole panel erroring.
 *
 * Streaming is abortable and re-runnable: switching customer (a new `path`) or calling `retry`
 * aborts any in-flight stream and starts fresh, so a timed-out or failed stream has a retry
 * affordance (task 14.6) and a customer switch never bleeds one customer's cards into another's.
 */

export type StreamStatus = 'streaming' | 'done' | 'error';

export interface AgentStreamState {
  /** Cards received so far, keyed by agent name. */
  readonly cards: ReadonlyMap<string, AgentCard>;
  readonly status: StreamStatus;
  /** Set when the stream failed to open (a 4xx/5xx or transport error), for the failure affordance. */
  readonly error: string | null;
  /** Re-open the stream from scratch. */
  readonly retry: () => void;
}

export interface AgentStreamOptions {
  /** Emit the `insights.first_card` RUM mark on the first card of this stream (task 10.5). */
  readonly markFirstCard?: boolean;
}

export function useAgentStream(
  path: string | null,
  options: AgentStreamOptions = {},
): AgentStreamState {
  const { markFirstCard = false } = options;
  const [cards, setCards] = useState<ReadonlyMap<string, AgentCard>>(new Map());
  const [status, setStatus] = useState<StreamStatus>('streaming');
  const [error, setError] = useState<string | null>(null);
  // Bumped by `retry` to force the effect to re-run and re-open the stream.
  const [attempt, setAttempt] = useState(0);
  const firstCardSeen = useRef(false);

  const retry = useCallback(() => {
    setAttempt((n) => n + 1);
  }, []);

  useEffect(() => {
    if (path === null) {
      return;
    }
    const streamPath = path;
    const controller = new AbortController();
    // Reset state for this (re)connection so stale cards never linger across a customer switch.
    // Deferred to a microtask so it is not a synchronous cascading render in the effect body.
    queueMicrotask(() => {
      setCards(new Map());
      setStatus('streaming');
      setError(null);
    });
    firstCardSeen.current = false;

    async function run(): Promise<void> {
      try {
        for await (const frame of openSse({ path: streamPath, signal: controller.signal })) {
          if (frame.event === 'agent') {
            const card = frame.data as AgentCard;
            if (markFirstCard && !firstCardSeen.current) {
              firstCardSeen.current = true;
              mark('insights.first_card');
            }
            setCards((prev) => {
              const next = new Map(prev);
              next.set(card.agent, card);
              return next;
            });
          } else if (frame.event === 'done') {
            setStatus('done');
          } else if (frame.event === 'error') {
            // A stream-level error frame (rare — most failures are contained as degraded cards).
            setStatus('error');
            setError('The AI stream reported an error.');
          }
        }
        // The generator ended without a `done` frame only on an aborted or dropped stream.
        setStatus((current) => (current === 'streaming' ? 'done' : current));
      } catch (cause: unknown) {
        if (controller.signal.aborted) {
          return;
        }
        setStatus('error');
        setError(messageFor(cause));
      }
    }

    void run();
    return () => {
      controller.abort();
    };
  }, [path, attempt, markFirstCard]);

  return { cards, status, error, retry };
}

function messageFor(cause: unknown): string {
  if (cause instanceof SseError) {
    if (cause.status === 404) {
      return 'This customer is not available.';
    }
    if (cause.status === 503) {
      return 'The AI layer is currently unavailable.';
    }
    return 'The AI stream could not be opened.';
  }
  return cause instanceof Error ? cause.message : 'The AI stream could not be opened.';
}
