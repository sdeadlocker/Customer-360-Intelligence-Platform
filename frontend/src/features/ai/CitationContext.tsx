import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import type { FactCitation, PassageCitation } from './types';

/**
 * Cross-widget citation inspection (task 14.2, requirements 10.8, 17.6).
 *
 * The two citation kinds resolve to two very different places, so they are handled apart:
 *
 * - A **fact citation** points at a field the deterministic widgets already render. Activating it
 *   scrolls the owning widget into view and highlights the field, so a number in an AI narrative is
 *   traceable to the exact deterministic value it was grounded in. Widgets register the DOM anchor
 *   for a citable field by `entity_type:field`, and the highlight is a transient CSS state cleared
 *   after a moment so it reads as a "look here" pulse, not a permanent selection.
 * - A **knowledge citation** points at a passage. Activating it opens the {@link PassageViewer} with
 *   the document title, section, version and effective date — the citation the backend already
 *   carries on the frame (design §8.4), so no extra fetch is needed to show provenance.
 *
 * This context is the shared plumbing; the actual viewer is mounted once at the dashboard root and
 * the highlight is a data attribute the CSS keys off, so every AI card and the ask panel share one
 * mechanism (design §9.1 — "the UI and Q&A share it").
 */

interface CitationContextValue {
  /** Scroll to and highlight the field a fact citation names. */
  readonly inspectFact: (citation: FactCitation) => void;
  /** Open the passage viewer for a knowledge citation. */
  readonly inspectPassage: (citation: PassageCitation) => void;
  /** The passage currently open in the viewer, or null. */
  readonly openPassage: PassageCitation | null;
  /** Close the passage viewer. */
  readonly closePassage: () => void;
  /** Register/deregister a widget's anchor element for a citable field key. */
  readonly registerAnchor: (key: string, element: HTMLElement | null) => void;
}

const CitationCtx = createContext<CitationContextValue | null>(null);

/** The DOM key a fact citation resolves to: entity type plus field, e.g. `financial_profile:net_worth_cents`. */
export function fieldKey(entityType: string, field: string): string {
  return `${entityType}:${field}`;
}

/** How long the "look here" highlight persists after a fact citation is clicked. */
const HIGHLIGHT_MS = 2400;

export function CitationProvider({
  children,
  onInspectFactWithoutAnchor,
}: {
  readonly children: ReactNode;
  /**
   * Called when a fact citation is activated but no widget anchor is registered for its field —
   * i.e. the AI surface is not on the customer's dashboard (the search-landing "Ask anything"
   * panel). The search page uses this to navigate to the cited customer's 360 view instead of
   * silently doing nothing, so a fact chip is a live "open this customer" link everywhere.
   */
  readonly onInspectFactWithoutAnchor?: (citation: FactCitation) => void;
}): React.JSX.Element {
  const [openPassage, setOpenPassage] = useState<PassageCitation | null>(null);
  // A mutable registry of field anchors. It is a ref, not state, because a registration must never
  // trigger a render — it only records where a citation should scroll to.
  const anchorsRef = useRef<Map<string, HTMLElement>>(new Map());
  // Held in a ref so a changing callback identity never rebuilds the context value (which would
  // remount consumers); the latest callback is synced in an effect and read at click time.
  const navigateRef = useRef(onInspectFactWithoutAnchor);
  useEffect(() => {
    navigateRef.current = onInspectFactWithoutAnchor;
  }, [onInspectFactWithoutAnchor]);

  const registerAnchor = useCallback((key: string, element: HTMLElement | null): void => {
    const anchors = anchorsRef.current;
    if (element === null) {
      anchors.delete(key);
    } else {
      anchors.set(key, element);
    }
  }, []);

  const inspectFact = useCallback((citation: FactCitation): void => {
    const anchor = anchorsRef.current.get(fieldKey(citation.entity_type, citation.field));
    if (anchor === undefined) {
      // No widget to scroll to (the cross-customer surface): fall back to navigation if provided.
      navigateRef.current?.(citation);
      return;
    }
    anchor.scrollIntoView({ behavior: 'smooth', block: 'center' });
    anchor.setAttribute('data-cite-highlight', 'true');
    window.setTimeout(() => {
      anchor.removeAttribute('data-cite-highlight');
    }, HIGHLIGHT_MS);
  }, []);

  const inspectPassage = useCallback((citation: PassageCitation): void => {
    setOpenPassage(citation);
  }, []);

  const closePassage = useCallback((): void => {
    setOpenPassage(null);
  }, []);

  const value = useMemo<CitationContextValue>(
    () => ({ inspectFact, inspectPassage, openPassage, closePassage, registerAnchor }),
    [inspectFact, inspectPassage, openPassage, closePassage, registerAnchor],
  );

  return <CitationCtx.Provider value={value}>{children}</CitationCtx.Provider>;
}

/** Access the citation context. Returns a no-op implementation outside a provider (safe for tests). */
export function useCitations(): CitationContextValue {
  const ctx = useContext(CitationCtx);
  if (ctx === null) {
    return NOOP;
  }
  return ctx;
}

const NOOP: CitationContextValue = {
  inspectFact: () => undefined,
  inspectPassage: () => undefined,
  openPassage: null,
  closePassage: () => undefined,
  registerAnchor: () => undefined,
};
