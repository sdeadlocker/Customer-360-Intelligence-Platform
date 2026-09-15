import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  acknowledgeSignal,
  dismissSignal,
  fetchWorklist,
  type Severity,
  type Signal,
  type SignalFilters,
  type SignalType,
} from './signalsApi';

/**
 * The signals worklist hook (task 17.6).
 *
 * Owns the ranked list, the type/severity filters, and the dismiss/ack actions. Loads the first
 * page on mount and whenever the filters change (an aborted in-flight request never overwrites a
 * newer one). A dismiss or ack removes the signal from the local list optimistically — the backend
 * suppresses it from subsequent reads anyway (dismissed within the cooling-off window, actioned
 * permanently), so dropping it immediately matches what a reload would show.
 *
 * The `announcement` string drives a single polite live region so a screen reader hears "N signals"
 * once per load and the outcome of an action, rather than a per-row readout (task 17.6).
 */

export type WorklistState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly signals: readonly Signal[] }
  /** The feed could not be loaded (e.g. detection has not run / a transient backend fault). Signals
   *  are a best-effort push feed, so this degrades to a quiet "unavailable" note rather than a
   *  page-dominating error — the rest of the landing page is unaffected. */
  | { readonly kind: 'unavailable' };

export interface Worklist {
  readonly state: WorklistState;
  readonly filters: SignalFilters;
  readonly announcement: string;
  readonly setTypes: (types: readonly SignalType[]) => void;
  readonly setMinSeverity: (severity: Severity | undefined) => void;
  readonly dismiss: (signalId: number) => void;
  readonly acknowledge: (signalId: number) => void;
  readonly reload: () => void;
}

export function useWorklist(): Worklist {
  const [state, setState] = useState<WorklistState>({ kind: 'loading' });
  const [filters, setFilters] = useState<SignalFilters>({});
  const [announcement, setAnnouncement] = useState('');
  const [reloadToken, setReloadToken] = useState(0);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    // Defer the loading transition out of the effect body so it is not a synchronous cascading
    // render (mirrors useCustomerSearch); the fetch resolves asynchronously regardless.
    queueMicrotask(() => {
      if (!controller.signal.aborted) {
        setState({ kind: 'loading' });
      }
    });

    fetchWorklist({ limit: 50, filters }, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) {
          return;
        }
        setState({ kind: 'ready', signals: page.items });
        setAnnouncement(
          page.items.length === 0
            ? 'No signals in your worklist.'
            : `${page.items.length} signal${page.items.length === 1 ? '' : 's'} in your worklist.`,
        );
      })
      .catch(() => {
        if (controller.signal.aborted) {
          return;
        }
        // The signals feed is best-effort (empty when detection has not run); a load failure
        // degrades quietly to "unavailable" rather than a red error that dominates the landing page.
        setState({ kind: 'unavailable' });
        setAnnouncement('The worklist is currently unavailable.');
      });

    return () => {
      controller.abort();
    };
  }, [filters, reloadToken]);

  const removeLocally = useCallback((signalId: number, outcome: string) => {
    setState((current) => {
      if (current.kind !== 'ready') {
        return current;
      }
      return { kind: 'ready', signals: current.signals.filter((s) => s.signal_id !== signalId) };
    });
    setAnnouncement(outcome);
  }, []);

  const dismiss = useCallback(
    (signalId: number) => {
      void dismissSignal(signalId)
        .then(() => {
          removeLocally(signalId, 'Signal dismissed.');
        })
        .catch(() => {
          setAnnouncement('The signal could not be dismissed.');
        });
    },
    [removeLocally],
  );

  const acknowledge = useCallback(
    (signalId: number) => {
      void acknowledgeSignal(signalId)
        .then(() => {
          removeLocally(signalId, 'Signal acknowledged.');
        })
        .catch(() => {
          setAnnouncement('The signal could not be acknowledged.');
        });
    },
    [removeLocally],
  );

  const setTypes = useCallback((types: readonly SignalType[]) => {
    setFilters((current) => {
      const { types: _omit, ...rest } = current;
      return types.length > 0 ? { ...rest, types } : rest;
    });
  }, []);

  const setMinSeverity = useCallback((severity: Severity | undefined) => {
    setFilters((current) => {
      const { minSeverity: _omit, ...rest } = current;
      return severity !== undefined ? { ...rest, minSeverity: severity } : rest;
    });
  }, []);

  const reload = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  return useMemo(
    () => ({
      state,
      filters,
      announcement,
      setTypes,
      setMinSeverity,
      dismiss,
      acknowledge,
      reload,
    }),
    [state, filters, announcement, setTypes, setMinSeverity, dismiss, acknowledge, reload],
  );
}
