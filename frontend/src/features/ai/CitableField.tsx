import { useEffect, useRef, type ReactNode } from 'react';

import { fieldKey, useCitations } from './CitationContext';

/**
 * Marks a deterministic widget field as the target a fact citation resolves to (task 14.2).
 *
 * A fact citation carries an `entity_type` and `field`; clicking it scrolls to and highlights the
 * matching field. This wrapper registers its DOM node under that `entity_type:field` key with the
 * shared citation context, so the AI cards and the ask panel can point at the exact deterministic
 * value a narrative was grounded in. The highlight is a transient `data-cite-highlight` attribute
 * the CSS pulses; the wrapper itself is a plain inline span that changes nothing about layout.
 *
 * Registration is keyed, so the last-mounted field for a key wins — which is what we want, since a
 * customer's dashboard shows each citable field once.
 */
export function CitableField({
  entityType,
  field,
  children,
}: {
  readonly entityType: string;
  readonly field: string;
  readonly children: ReactNode;
}): React.JSX.Element {
  const ref = useRef<HTMLSpanElement>(null);
  const { registerAnchor } = useCitations();
  const key = fieldKey(entityType, field);

  useEffect(() => {
    registerAnchor(key, ref.current);
    return () => {
      registerAnchor(key, null);
    };
  }, [key, registerAnchor]);

  return (
    <span ref={ref} className="citable-field" data-cite-field={key}>
      {children}
    </span>
  );
}
