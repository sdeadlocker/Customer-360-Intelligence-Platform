import { useEffect, useState } from 'react';

import { ApiError, getEnvelope } from '../../../api/client';
import type { Meta } from '../../../api/types';

/**
 * Fetch state for a customer sub-resource that is not part of the composed 360 payload.
 *
 * Several Phase 13 widgets need data the aggregator does not compose — holdings, the credit profile,
 * the relationship graph, engagement history — each of which has its own entitlement-scoped, masked
 * endpoint. This hook fetches one such envelope for the current customer, cancels in flight on a
 * customer switch, and exposes the loaded data together with the envelope `meta` so a widget can
 * derive `restricted` from `meta.masked_fields` the same way the 360 widgets do.
 */
export type SubResourceState<T> =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly message: string; readonly correlationId: string | null }
  | { readonly kind: 'ready'; readonly data: T; readonly meta: Meta };

export function useSubResource<T>(path: string): SubResourceState<T> {
  const [state, setState] = useState<SubResourceState<T>>({ kind: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    // Reset to loading in a microtask so it is not a synchronous cascading render in the effect body
    // (mirrors DashboardPage; keeps the react-hooks/set-state-in-effect rule satisfied).
    queueMicrotask(() => {
      setState({ kind: 'loading' });
    });
    void getEnvelope<T>(path, controller.signal)
      .then((envelope) => {
        setState({ kind: 'ready', data: envelope.data, meta: envelope.meta });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        if (cause instanceof ApiError) {
          setState({ kind: 'error', message: cause.message, correlationId: cause.correlationId });
          return;
        }
        const message = cause instanceof Error ? cause.message : 'Unknown error';
        setState({ kind: 'error', message, correlationId: null });
      });
    return () => {
      controller.abort();
    };
  }, [path]);

  return state;
}
