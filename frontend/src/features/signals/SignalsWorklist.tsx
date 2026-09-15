import { useId } from 'react';

import type { Severity, Signal, SignalType } from './signalsApi';
import { useWorklist } from './useWorklist';

/**
 * The proactive signals worklist (task 17.6, requirements 8.1–8.7, 16.x).
 *
 * A prioritized daily queue rendered beside Ask-AI on the landing page: ranked cards each showing
 * severity, the customer, the signal type and value-free evidence chips, with dismiss / acknowledge
 * actions and drill-through to the customer's full 360 view. Filters narrow by signal type and
 * minimum severity.
 *
 * Accessibility (task 17.6):
 * - the queue is an ordered list of buttons/links, fully keyboard-navigable;
 * - a single polite live region announces the count on load and the outcome of an action, once,
 *   rather than a per-row readout;
 * - severity is encoded by a text label and an icon glyph, never colour alone — the colour is
 *   decorative reinforcement on top of the label.
 */

const SIGNAL_TYPE_LABEL: Record<SignalType, string> = {
  RISK_BAND_UP: 'Risk rising',
  AML_PEP_FLAG: 'Compliance flag',
  LARGE_DEPOSIT: 'Large deposit',
  LIFE_EVENT: 'Life event',
};

const SEVERITY_LABEL: Record<Severity, string> = {
  INFO: 'Info',
  WARNING: 'Warning',
  CRITICAL: 'Critical',
};

const SEVERITY_GLYPH: Record<Severity, string> = {
  INFO: 'ℹ',
  WARNING: '▲',
  CRITICAL: '⛔',
};

const SIGNAL_TYPES: readonly SignalType[] = [
  'RISK_BAND_UP',
  'AML_PEP_FLAG',
  'LARGE_DEPOSIT',
  'LIFE_EVENT',
];

const SEVERITIES: readonly Severity[] = ['INFO', 'WARNING', 'CRITICAL'];

/** Render a value-at-stake that may be an integer-cents number or a banded label string. */
function renderValue(value: number | string | undefined): string | null {
  if (value === undefined) {
    return null;
  }
  if (typeof value === 'string') {
    return value; // already a band label from masking
  }
  if (value <= 0) {
    return null;
  }
  // Cents to a whole-dollar display; no float maths beyond this presentational division.
  const dollars = Math.round(value / 100);
  return `$${dollars.toLocaleString()}`;
}

export function SignalsWorklist({
  onOpenCustomer,
}: {
  readonly onOpenCustomer: (customerId: string) => void;
}): React.JSX.Element {
  const worklist = useWorklist();
  const typeSelectId = useId();
  const severitySelectId = useId();

  const activeTypes = worklist.filters.types ?? [];

  const toggleType = (type: SignalType): void => {
    const next = activeTypes.includes(type)
      ? activeTypes.filter((t) => t !== type)
      : [...activeTypes, type];
    worklist.setTypes(next);
  };

  return (
    <section className="module signals-worklist" aria-labelledby="signals-worklist-title">
      <div className="module__head">
        <h2 id="signals-worklist-title" className="module__title">
          Your worklist
        </h2>
        <button
          type="button"
          className="button"
          onClick={worklist.reload}
          aria-label="Refresh worklist"
        >
          Refresh
        </button>
      </div>

      <div className="module__body">
        {/* One polite, atomic status line — announced once per load/action, not per row. */}
        <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {worklist.announcement}
        </p>

        <fieldset className="signals-worklist__filters">
          <legend className="sr-only">Filter signals</legend>
          <div
            className="signals-worklist__type-filters"
            role="group"
            aria-label="Filter by signal type"
            id={typeSelectId}
          >
            {SIGNAL_TYPES.map((type) => {
              const active = activeTypes.includes(type);
              return (
                <button
                  key={type}
                  type="button"
                  className={`chip${active ? ' chip--active' : ''}`}
                  aria-pressed={active}
                  onClick={() => {
                    toggleType(type);
                  }}
                >
                  {SIGNAL_TYPE_LABEL[type]}
                </button>
              );
            })}
          </div>
          <label htmlFor={severitySelectId} className="signals-worklist__severity-label">
            Minimum severity
            <select
              id={severitySelectId}
              className="signals-worklist__severity-select"
              value={worklist.filters.minSeverity ?? ''}
              onChange={(event) => {
                const value = event.target.value;
                worklist.setMinSeverity(value === '' ? undefined : (value as Severity));
              }}
            >
              <option value="">Any</option>
              {SEVERITIES.map((severity) => (
                <option key={severity} value={severity}>
                  {SEVERITY_LABEL[severity]}
                </option>
              ))}
            </select>
          </label>
        </fieldset>

        {worklist.state.kind === 'loading' && <p className="module__empty">Loading worklist…</p>}

        {worklist.state.kind === 'unavailable' && (
          <p className="module__empty">
            The worklist is currently unavailable. Run detection (
            <span className="mono">c360 detect-signals</span>) to populate it, then Refresh.
          </p>
        )}

        {worklist.state.kind === 'ready' && worklist.state.signals.length === 0 && (
          <p className="module__empty">
            No signals right now. Run detection or adjust the filters to see your worklist.
          </p>
        )}

        {worklist.state.kind === 'ready' && worklist.state.signals.length > 0 && (
          <ol className="signals-worklist__list">
            {worklist.state.signals.map((signal) => (
              <SignalCard
                key={signal.signal_id}
                signal={signal}
                onOpen={() => {
                  onOpenCustomer(signal.customer_id);
                }}
                onDismiss={() => {
                  worklist.dismiss(signal.signal_id);
                }}
                onAcknowledge={() => {
                  worklist.acknowledge(signal.signal_id);
                }}
              />
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

function SignalCard({
  signal,
  onOpen,
  onDismiss,
  onAcknowledge,
}: {
  readonly signal: Signal;
  readonly onOpen: () => void;
  readonly onDismiss: () => void;
  readonly onAcknowledge: () => void;
}): React.JSX.Element {
  const value = renderValue(signal.value_at_stake_cents);
  const severityClass = `signals-worklist__severity signals-worklist__severity--${signal.severity.toLowerCase()}`;

  return (
    <li className="signals-worklist__card">
      <div className="signals-worklist__card-head">
        <span className={severityClass}>
          <span aria-hidden="true" className="signals-worklist__severity-glyph">
            {SEVERITY_GLYPH[signal.severity]}
          </span>
          {SEVERITY_LABEL[signal.severity]}
        </span>
        <span className="signals-worklist__type">{SIGNAL_TYPE_LABEL[signal.signal_type]}</span>
      </div>

      <p className="signals-worklist__summary">{signal.evidence.summary}</p>

      <div className="signals-worklist__chips">
        <button
          type="button"
          className="signals-worklist__customer-link focus-ring"
          onClick={onOpen}
        >
          Customer {signal.customer_id}
        </button>
        {value !== null && <span className="chip chip--muted">{value}</span>}
        {Object.entries(signal.evidence.details ?? {})
          .filter(([key]) => key !== 'segment')
          .map(([key, val]) => (
            <span key={key} className="chip chip--muted">
              {val}
            </span>
          ))}
      </div>

      <div className="signals-worklist__actions">
        <button type="button" className="button" onClick={onOpen}>
          Open 360
        </button>
        <button type="button" className="button" onClick={onAcknowledge}>
          Acknowledge
        </button>
        <button type="button" className="button" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </li>
  );
}
