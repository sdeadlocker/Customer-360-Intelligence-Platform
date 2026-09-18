import { useCallback, useEffect, useRef, useState } from 'react';

import { openSse, SseError } from '../../api/sse';
import type { FactCitation, PassageCitation, QaCitations, QaDone } from './types';

/**
 * Drive the ask-anything conversation for one customer (task 14.5; backend task 9.5).
 *
 * The hook has two modes, keyed on whether a `customerId` is given:
 *
 * - **Single-customer** (a `customerId`, from a customer's dashboard): a question POSTs to
 *   `/customers/{id}/ask`. Conversation memory is per customer session and cleared on switch
 *   (requirement 11.9): a new `customerId` resets the turns and aborts any in-flight stream, and
 *   the backend deletes the prior thread server-side keyed by `(session_id, customer_id)`.
 * - **Cross-customer** (`customerId` omitted, the search-landing "Ask anything" surface): the
 *   question POSTs to `/ask` and the agent resolves which customer it is about itself, searching
 *   across every customer the signed-in user is entitled to (backend task 9.6).
 *
 * Either way the answer streams back as `token`, `citations` and `done` frames
 * (`c360/agents/qa_streaming.py`). This hook owns the conversation turns: the streaming answer
 * accumulates from `token` frames, the fact and knowledge citations are captured separately from
 * the `citations` frame (design §9.1), any graph traversal paths are captured for display
 * (task 9.4), and the `done` frame's behavioural flags — `refused`, `no_guidance`, `degraded` —
 * are surfaced so the UI renders a refusal or "no guidance found" plainly rather than as a normal
 * answer (requirement 11.6, 17.10). A stable per-mount `session_id` threads the multi-turn memory
 * the backend checkpointer keys on.
 */

export type TurnStatus = 'streaming' | 'complete' | 'error';

export interface Turn {
  readonly id: number;
  readonly question: string;
  readonly answer: string;
  readonly factCitations: readonly FactCitation[];
  readonly passageCitations: readonly PassageCitation[];
  readonly traversalPaths: readonly (readonly string[])[];
  readonly status: TurnStatus;
  readonly refused: boolean;
  readonly noGuidance: boolean;
  readonly degraded: boolean;
  readonly errorMessage: string | null;
}

export interface AskState {
  readonly turns: readonly Turn[];
  /** True while a turn is streaming; the composer disables send to keep one turn in flight. */
  readonly busy: boolean;
  readonly ask: (question: string) => void;
  /** Clear the conversation and abort any in-flight stream (used when collapsing the panel). */
  readonly reset: () => void;
}

/** A per-mount conversation session id; stable across turns, fresh on remount. */
function newSessionId(): string {
  const rand = Math.random().toString(36).slice(2);
  return `web-${Date.now().toString(36)}-${rand}`;
}

export function useAskStream(customerId?: string): AskState {
  const [turns, setTurns] = useState<readonly Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const sessionId = useRef<string>(newSessionId());
  const controllerRef = useRef<AbortController | null>(null);
  const nextId = useRef(0);

  // A customer switch clears the conversation and aborts any in-flight stream (requirement 11.9).
  // On the cross-customer surface `customerId` is undefined and stable, so this fires once on mount.
  // The reset is deferred to a microtask so it is not a synchronous cascading render in the effect.
  useEffect(() => {
    sessionId.current = newSessionId();
    queueMicrotask(() => {
      setTurns([]);
      setBusy(false);
    });
    return () => {
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
  }, [customerId]);

  const patch = useCallback((id: number, change: Partial<Turn>): void => {
    setTurns((prev) => prev.map((turn) => (turn.id === id ? { ...turn, ...change } : turn)));
  }, []);

  const ask = useCallback(
    (question: string): void => {
      const trimmed = question.trim();
      if (trimmed === '' || busy) {
        return;
      }
      const id = nextId.current++;
      const controller = new AbortController();
      controllerRef.current = controller;
      setBusy(true);
      setTurns((prev) => [
        ...prev,
        {
          id,
          question: trimmed,
          answer: '',
          factCitations: [],
          passageCitations: [],
          traversalPaths: [],
          status: 'streaming',
          refused: false,
          noGuidance: false,
          degraded: false,
          errorMessage: null,
        },
      ]);

      async function run(): Promise<void> {
        try {
          let answer = '';
          const path =
            customerId === undefined ? '/ask' : `/customers/${encodeURIComponent(customerId)}/ask`;
          for await (const frame of openSse({
            path,
            method: 'POST',
            body: { question: trimmed, session_id: sessionId.current },
            signal: controller.signal,
          })) {
            if (frame.event === 'token') {
              const { text } = frame.data as { text: string };
              answer += text;
              patch(id, { answer });
            } else if (frame.event === 'citations') {
              const c = frame.data as QaCitations;
              patch(id, {
                factCitations: c.fact_citations,
                passageCitations: c.passage_citations,
                traversalPaths: c.traversal_paths,
              });
            } else if (frame.event === 'done') {
              const d = frame.data as QaDone;
              patch(id, {
                status: 'complete',
                refused: d.refused,
                noGuidance: d.no_guidance,
                degraded: d.degraded,
              });
            }
          }
        } catch (cause: unknown) {
          if (controller.signal.aborted) {
            return;
          }
          patch(id, {
            status: 'error',
            errorMessage: messageFor(cause, customerId !== undefined),
          });
        } finally {
          if (!controller.signal.aborted) {
            setBusy(false);
          }
        }
      }

      void run();
    },
    [busy, customerId, patch],
  );

  const reset = useCallback((): void => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    sessionId.current = newSessionId();
    setTurns([]);
    setBusy(false);
  }, []);

  return { turns, busy, ask, reset };
}

function messageFor(cause: unknown, customerScoped: boolean): string {
  if (cause instanceof SseError) {
    if (cause.status === 404) {
      // On the customer-scoped path a 404 means that customer is not visible; on the cross-customer
      // path there is no customer in the URL, so a 404 is the endpoint being unreachable.
      return customerScoped
        ? 'This customer is not available.'
        : 'The assistant is currently unavailable.';
    }
    if (cause.status === 503) {
      return 'The assistant is currently unavailable.';
    }
  }
  return cause instanceof Error ? cause.message : 'The question could not be answered.';
}
