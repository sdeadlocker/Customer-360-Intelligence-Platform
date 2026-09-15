import type { ReactNode } from 'react';

import type { ModuleError } from '../api/types';

/**
 * The shared module state machine (task 12.5, requirements 4.3, 4.4, 4.7, 12.4).
 *
 * Every dashboard widget shows data that can be present, absent, partially masked, entirely
 * off-limits, or failed to load — and those five outcomes must look visually distinct so a user is
 * never left guessing whether a blank panel means "no data", "not allowed to see it" or "the server
 * broke". This wrapper is the single place those states are rendered, so all of Phase 13's widgets
 * inherit the same vocabulary:
 *
 * - `loading`    — the fetch is in flight.
 * - `ready`      — data is present and complete; the wrapper renders its children.
 * - `partial`    — data is present but the module reported an error for part of it (`meta.errors[]`),
 *                  so children render alongside a non-blocking notice. This is the aggregator's
 *                  partial-200 (requirement 4.4).
 * - `restricted` — the caller is entitled to the customer but not to some field(s) in this module;
 *                  driven by `meta.masked_fields`. Children still render (the visible fields), with
 *                  a "restricted" badge — the module is not empty, it is redacted (requirement 12.4).
 * - `error`      — the fetch failed outright; children do not render.
 *
 * "No data", "not entitled" and "failed" are the three that must not be confused (requirement 4.7):
 * an empty `ready`, a `restricted`, and an `error` respectively, each with its own treatment.
 */

export type ModuleStatus = 'loading' | 'ready' | 'partial' | 'restricted' | 'error';

export interface ModuleStateModel {
  readonly status: ModuleStatus;
  /** Redacted field paths for this module, from `meta.masked_fields`. Non-empty ⇒ restricted. */
  readonly maskedFields?: readonly string[];
  /** Partial-failure notices for this module, from `meta.errors[]`. Non-empty ⇒ partial. */
  readonly errors?: readonly ModuleError[];
  /** A user-facing message for the `error` state. */
  readonly errorMessage?: string;
  /** Correlation ID for an `error`, so a user can report it and an engineer can find the trace. */
  readonly correlationId?: string | null;
}

/**
 * Derive the module status from a resolved envelope's meta, scoped to one module.
 *
 * A widget passes the `meta.masked_fields` and `meta.errors` entries that belong to it (filtered by
 * the module's field prefix, e.g. `profile.`), and gets back the state to render. Ordering matters:
 * an outright failure is `error`; otherwise a partial-failure notice makes it `partial`; otherwise a
 * masked field makes it `restricted`; otherwise it is `ready`.
 */
export function deriveModuleStatus(input: {
  readonly failed?: boolean;
  readonly maskedFields?: readonly string[];
  readonly errors?: readonly ModuleError[];
}): ModuleStatus {
  if (input.failed === true) {
    return 'error';
  }
  if (input.errors !== undefined && input.errors.length > 0) {
    return 'partial';
  }
  if (input.maskedFields !== undefined && input.maskedFields.length > 0) {
    return 'restricted';
  }
  return 'ready';
}

export interface ModuleStateProps {
  readonly title: string;
  readonly state: ModuleStateModel;
  /** Rendered for `ready`, `partial` and `restricted` — the states where visible data exists. */
  readonly children: ReactNode;
  /** Optional custom content for the empty-`ready` case; defaults to a plain "no data" line. */
  readonly emptyLabel?: string;
  /** Whether the module has zero rows even though the fetch succeeded (drives the "no data" state). */
  readonly isEmpty?: boolean;
}

/**
 * The reusable module wrapper. A widget wraps its content in `<ModuleState>` and the correct chrome
 * (spinner, badge, notice, error) is applied for it. It is a `section` with an accessible label and
 * an `aria-live` region for the async status so a screen reader is told when a module finishes
 * loading, fails, or comes back restricted (a nod to Phase 15's live-region work).
 */
export function ModuleState({
  title,
  state,
  children,
  emptyLabel = 'No data available.',
  isEmpty = false,
}: ModuleStateProps): React.JSX.Element {
  const { status } = state;

  return (
    <section
      className="module"
      aria-labelledby={`module-${slug(title)}-title`}
      data-status={status}
    >
      <div className="module__head">
        <h2 id={`module-${slug(title)}-title`} className="module__title">
          {title}
        </h2>
        {status === 'restricted' && (
          <span className="badge badge--restricted" title="Some fields are hidden for your role">
            Restricted
          </span>
        )}
        {status === 'partial' && (
          <span className="badge badge--partial" title="Some data could not be loaded">
            Partial
          </span>
        )}
      </div>

      <div className="module__body" aria-live="polite" aria-busy={status === 'loading'}>
        {status === 'loading' && (
          <p role="status" className="module__loading">
            Loading {title.toLowerCase()}…
          </p>
        )}

        {status === 'error' && (
          <div role="alert" className="module__error">
            <p>{state.errorMessage ?? 'This module could not be loaded.'}</p>
            {state.correlationId != null && state.correlationId !== '' && (
              <p className="mono module__correlation">Reference: {state.correlationId}</p>
            )}
          </div>
        )}

        {(status === 'ready' || status === 'partial' || status === 'restricted') && (
          <>
            {isEmpty ? <p className="module__empty">{emptyLabel}</p> : children}
            {status === 'partial' && state.errors && state.errors.length > 0 && (
              <ul className="module__notices">
                {state.errors.map((error) => (
                  <li key={`${error.module}:${error.code}`} className="module__notice">
                    {error.message}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');
}
