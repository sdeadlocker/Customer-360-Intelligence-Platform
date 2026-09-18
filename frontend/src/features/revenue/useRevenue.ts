import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  PREVIEW_ECONOMIC_PROFIT,
  PREVIEW_FEE_RECOVERY,
  PREVIEW_INFLOWS,
  PREVIEW_PIPELINE,
  PREVIEW_WALLET_SHARE,
} from './previewData';
import {
  acknowledgeInflow,
  dismissInflow,
  fetchCustomerInflows,
  fetchEconomicProfit,
  fetchFeeRecovery,
  fetchMoneyInMotion,
  fetchPipeline,
  fetchWalletShare,
  type EconomicProfit,
  type FeeRecovery,
  type MoneyInMotionEvent,
  type OpportunityPipeline,
  type WalletShare,
} from './revenueApi';

/**
 * Data hooks for the four revenue plays (Phase 22).
 *
 * Each follows the established best-effort feed pattern from {@link ../signals/useWorklist}: abort
 * the previous request when the dependency changes, defer the loading transition into a microtask so
 * the effect body performs no synchronous cascading render, and degrade rather than throw.
 *
 * Where the signals worklist degrades to `unavailable`, these degrade to **`preview`**: the revenue
 * detection job and its endpoints are not built yet, so a failed fetch renders the illustrative
 * fixtures in {@link ./previewData} behind a visible Preview badge. That keeps the layout reviewable
 * today without ever presenting an ungrounded number as if it were real. When the backend lands,
 * delete the `preview` branch here and `previewData.ts`; the ready path is already correct.
 */

export type RevenueState<T> =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly data: T }
  /** The endpoint is absent or failed; `data` is an illustrative layout preview, clearly labelled. */
  | { readonly kind: 'preview'; readonly data: T };

/** Is this state showing illustrative rather than grounded figures? */
export function isPreview<T>(state: RevenueState<T>): boolean {
  return state.kind === 'preview';
}

/**
 * Shared single-fetch machinery: load once per dependency change, fall back to `previewValue` on any
 * failure.
 *
 * `load` must be referentially stable — callers wrap it in `useCallback` keyed on whatever it closes
 * over (a customer id) — and `previewValue` must be a module-level constant. Both are therefore safe
 * effect dependencies, which is why no refs are involved. `nonce` exists so a caller can force a
 * refetch without changing the loader's identity.
 */
function useRevenueResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  previewValue: T,
  nonce: number,
): RevenueState<T> {
  const [state, setState] = useState<RevenueState<T>>({ kind: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    // Defer the loading transition out of the effect body so it is not a synchronous cascading
    // render (mirrors useSubResource and useWorklist); the fetch resolves asynchronously regardless.
    queueMicrotask(() => {
      if (!controller.signal.aborted) {
        setState({ kind: 'loading' });
      }
    });

    load(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ kind: 'ready', data });
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setState({ kind: 'preview', data: previewValue });
        }
      });

    return () => {
      controller.abort();
    };
  }, [load, previewValue, nonce]);

  return state;
}

// ---------------------------------------------------------------- play 8: pipeline (bank-wide)

export interface Pipeline {
  readonly state: RevenueState<OpportunityPipeline>;
  readonly announcement: string;
  readonly reload: () => void;
}

/** The caller's entitlement-scoped priced pipeline across all four plays. */
export function usePipeline(): Pipeline {
  const [reloadToken, setReloadToken] = useState(0);
  const load = useCallback((signal: AbortSignal) => fetchPipeline(signal), []);
  const state = useRevenueResource(load, PREVIEW_PIPELINE, reloadToken);

  const reload = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  const announcement = useMemo(() => {
    if (state.kind === 'loading') {
      return '';
    }
    const prefix = state.kind === 'preview' ? 'Preview figures. ' : '';
    return `${prefix}${String(state.data.plays.length)} revenue plays, ${String(
      state.data.urgent_count,
    )} opportunities needing action today.`;
  }, [state]);

  return useMemo(() => ({ state, announcement, reload }), [state, announcement, reload]);
}

// ---------------------------------------------------------------- play 5: money in motion

export interface MoneyInMotion {
  readonly state: RevenueState<readonly MoneyInMotionEvent[]>;
  readonly announcement: string;
  readonly acknowledge: (eventId: number) => void;
  readonly dismiss: (eventId: number) => void;
  readonly reload: () => void;
}

/**
 * Money-in-motion events. Pass a `customerId` for the single-customer card, or omit it for the
 * cross-book landing-page feed.
 */
export function useMoneyInMotion(customerId?: string): MoneyInMotion {
  const [reloadToken, setReloadToken] = useState(0);
  const [removed, setRemoved] = useState<readonly number[]>([]);
  const [actionNote, setActionNote] = useState('');

  const load = useCallback(
    (signal: AbortSignal) =>
      customerId === undefined
        ? fetchMoneyInMotion({ limit: 25 }, signal)
        : fetchCustomerInflows(customerId, signal),
    [customerId],
  );
  const fetched = useRevenueResource<readonly MoneyInMotionEvent[]>(
    load,
    PREVIEW_INFLOWS,
    reloadToken,
  );

  // Acting on an event removes it locally: the backend suppresses an actioned or dismissed event
  // from subsequent reads, so dropping it immediately matches what a reload would show.
  const state = useMemo<RevenueState<readonly MoneyInMotionEvent[]>>(() => {
    if (fetched.kind === 'loading' || removed.length === 0) {
      return fetched;
    }
    const visible = fetched.data.filter((event) => !removed.includes(event.event_id));
    return { kind: fetched.kind, data: visible };
  }, [fetched, removed]);

  const remove = useCallback((eventId: number) => {
    setRemoved((current) => (current.includes(eventId) ? current : [...current, eventId]));
  }, []);

  const acknowledge = useCallback(
    (eventId: number) => {
      // With the endpoint absent the optimistic removal is the whole interaction, and the
      // announcement says so plainly rather than implying a persisted change.
      void acknowledgeInflow(eventId)
        .then(() => {
          setActionNote('Outreach logged.');
        })
        .catch(() => {
          setActionNote(
            'Outreach logged in this session only; the revenue service is unavailable.',
          );
        })
        .finally(() => {
          remove(eventId);
        });
    },
    [remove],
  );

  const dismiss = useCallback(
    (eventId: number) => {
      void dismissInflow(eventId)
        .then(() => {
          setActionNote('Event dismissed.');
        })
        .catch(() => {
          setActionNote(
            'Event dismissed in this session only; the revenue service is unavailable.',
          );
        })
        .finally(() => {
          remove(eventId);
        });
    },
    [remove],
  );

  const reload = useCallback(() => {
    setRemoved([]);
    setActionNote('');
    setReloadToken((token) => token + 1);
  }, []);

  const announcement = useMemo(() => {
    if (actionNote !== '') {
      return actionNote;
    }
    if (state.kind === 'loading') {
      return '';
    }
    const prefix = state.kind === 'preview' ? 'Preview figures. ' : '';
    const count = state.data.length;
    if (count === 0) {
      return `${prefix}No money-in-motion events.`;
    }
    return `${prefix}${String(count)} money-in-motion event${count === 1 ? '' : 's'} awaiting outreach.`;
  }, [state, actionNote]);

  return useMemo(
    () => ({ state, announcement, acknowledge, dismiss, reload }),
    [state, announcement, acknowledge, dismiss, reload],
  );
}

// ---------------------------------------------------------------- play 4: wallet share

export function useWalletShare(customerId: string): RevenueState<WalletShare> {
  const load = useCallback(
    (signal: AbortSignal) => fetchWalletShare(customerId, signal),
    [customerId],
  );
  return useRevenueResource(load, PREVIEW_WALLET_SHARE, 0);
}

// ---------------------------------------------------------------- play 6: fee recovery

export function useFeeRecovery(customerId: string): RevenueState<FeeRecovery> {
  const load = useCallback(
    (signal: AbortSignal) => fetchFeeRecovery(customerId, signal),
    [customerId],
  );
  return useRevenueResource(load, PREVIEW_FEE_RECOVERY, 0);
}

// ---------------------------------------------------------------- play 8: economic profit

export function useEconomicProfit(customerId: string): RevenueState<EconomicProfit> {
  const load = useCallback(
    (signal: AbortSignal) => fetchEconomicProfit(customerId, signal),
    [customerId],
  );
  return useRevenueResource(load, PREVIEW_ECONOMIC_PROFIT, 0);
}
