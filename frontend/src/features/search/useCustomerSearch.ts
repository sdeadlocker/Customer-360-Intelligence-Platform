import { useEffect, useRef, useState } from 'react';

import { ApiError, searchCustomers } from '../../api/client';
import type { CustomerSearchHit } from '../../api/types';

/**
 * Debounced customer typeahead (requirement 3.1, 3.2).
 *
 * Search fires only once the query reaches three characters and after a short debounce, so a fast
 * typist makes one request rather than one per keystroke — the way the sub-500ms budget is actually
 * met on the wire. Each query supersedes the last: an in-flight request for a stale query is aborted
 * so its late response can never overwrite the results of a newer one (the classic out-of-order race
 * a naive debounce leaves open).
 */

export type SearchState =
  | { readonly kind: 'idle' } // fewer than the minimum characters typed
  | { readonly kind: 'loading' }
  | { readonly kind: 'results'; readonly hits: readonly CustomerSearchHit[] }
  | { readonly kind: 'error'; readonly message: string };

const MIN_CHARS = 3;
const DEBOUNCE_MS = 200;

/** The minimum character count before a search fires, exposed for the UI's guidance copy. */
export const SEARCH_MIN_CHARS = MIN_CHARS;

export function useCustomerSearch(query: string): SearchState {
  const [state, setState] = useState<SearchState>({ kind: 'idle' });
  const controllerRef = useRef<AbortController | null>(null);

  const trimmed = query.trim();

  useEffect(() => {
    // Cancel any request still in flight from the previous query.
    controllerRef.current?.abort();

    if (trimmed.length < MIN_CHARS) {
      // Below the threshold there is nothing to search; reset to idle in a microtask so the reset
      // is not a synchronous cascading render inside the effect body.
      queueMicrotask(() => {
        setState({ kind: 'idle' });
      });
      return;
    }

    const controller = new AbortController();
    controllerRef.current = controller;

    const timer = setTimeout(() => {
      setState({ kind: 'loading' });
      searchCustomers({ q: trimmed, limit: 25 }, controller.signal)
        .then((envelope) => {
          if (!controller.signal.aborted) {
            setState({ kind: 'results', hits: envelope.data.items ?? [] });
          }
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) {
            return;
          }
          const message =
            cause instanceof ApiError
              ? cause.message
              : cause instanceof Error
                ? cause.message
                : 'Search failed';
          setState({ kind: 'error', message });
        });
    }, DEBOUNCE_MS);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [trimmed]);

  return state;
}

/** Whether the query has content but is still below the search threshold, for the guidance copy. */
export function belowSearchMinimum(query: string): boolean {
  const trimmed = query.trim();
  return trimmed.length > 0 && trimmed.length < MIN_CHARS;
}
