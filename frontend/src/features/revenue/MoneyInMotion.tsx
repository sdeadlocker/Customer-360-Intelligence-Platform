import { ModuleState } from '../../components/ModuleState';
import { formatCents } from '../dashboard/widgets/format';
import {
  formatConfidence,
  INFLOW_SOURCE_LABEL,
  PreviewNote,
  urgencyOf,
  URGENCY_LABEL,
} from './RevenueChrome';
import type { MoneyInMotionEvent } from './revenueApi';
import { useMoneyInMotion } from './useRevenue';

/**
 * Money in motion (play 5) — the highest-conversion moment in banking, made visible.
 *
 * A large unexpected inflow (property sale, bonus, business exit, inheritance) sits in a checking
 * account for days, not weeks. Detected from the same transaction-anomaly layer the signals feed
 * already uses, each event is presented with the one thing that actually decides whether the money
 * stays: **how much of the action window is left**. A call on day one converts; the same call two
 * weeks later converts nothing.
 *
 * Two surfaces share the card:
 *  - {@link MoneyInMotionPanel} — the cross-book feed on the landing page, so an RM sees the whole
 *    book's urgent events before opening any profile.
 *  - {@link MoneyInMotionWidget} — the same events scoped to the open customer, on the dashboard.
 *
 * Urgency is never colour-only (requirement 16.1): every card states its tier and its day count in
 * text, and the meter is decorative reinforcement.
 */

/** The anchor the hero's "Review now" button scrolls to. */
export const MONEY_IN_MOTION_ANCHOR = 'money-in-motion';

export function MoneyInMotionPanel({
  onOpenCustomer,
}: {
  readonly onOpenCustomer: (customerId: string) => void;
}): React.JSX.Element {
  const feed = useMoneyInMotion();
  const preview = feed.state.kind === 'preview';

  return (
    <section
      id={MONEY_IN_MOTION_ANCHOR}
      className="module money-in-motion"
      aria-labelledby="money-in-motion-title"
    >
      <div className="module__head">
        <h2 id="money-in-motion-title" className="module__title">
          <span className="money-in-motion__title-glyph" aria-hidden="true">
            💸
          </span>
          Money in motion
        </h2>
        <button
          type="button"
          className="button"
          onClick={feed.reload}
          aria-label="Refresh money-in-motion events"
        >
          Refresh
        </button>
      </div>

      <div className="module__body">
        <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {feed.announcement}
        </p>

        <p className="money-in-motion__lede">
          Large inflows still inside the window where they can be retained. Ranked by how soon the
          window closes.
        </p>

        {preview && <PreviewNote what="money-in-motion detection" />}

        {feed.state.kind === 'loading' && (
          <p className="module__empty">Loading money-in-motion events…</p>
        )}

        {feed.state.kind !== 'loading' && feed.state.data.length === 0 && (
          <p className="module__empty">
            No inflow events awaiting outreach. Run detection (
            <span className="mono">c360 detect-revenue</span>) to refresh, then Refresh.
          </p>
        )}

        {feed.state.kind !== 'loading' && feed.state.data.length > 0 && (
          <ol className="money-in-motion__list">
            {[...feed.state.data]
              .sort((a, b) => a.days_remaining - b.days_remaining)
              .map((event) => (
                <InflowCard
                  key={event.event_id}
                  event={event}
                  showCustomer
                  onOpen={() => {
                    onOpenCustomer(event.customer_id);
                  }}
                  onAcknowledge={() => {
                    feed.acknowledge(event.event_id);
                  }}
                  onDismiss={() => {
                    feed.dismiss(event.event_id);
                  }}
                />
              ))}
          </ol>
        )}
      </div>
    </section>
  );
}

/** The same feed scoped to the open customer, mounted as a dashboard card. */
export function MoneyInMotionWidget({
  customerId,
}: {
  readonly customerId: string;
}): React.JSX.Element {
  const feed = useMoneyInMotion(customerId);
  const preview = feed.state.kind === 'preview';
  const loading = feed.state.kind === 'loading';
  const events = loading ? [] : feed.state.data;

  return (
    <ModuleState
      title="Money in motion"
      state={{ status: loading ? 'loading' : 'ready' }}
      isEmpty={!loading && events.length === 0}
      emptyLabel="No large inflows detected in the current window."
    >
      <div className="widget money-in-motion money-in-motion--scoped">
        <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {feed.announcement}
        </p>
        {preview && <PreviewNote what="money-in-motion detection" />}
        <ol className="money-in-motion__list">
          {[...events]
            .sort((a, b) => a.days_remaining - b.days_remaining)
            .map((event) => (
              <InflowCard
                key={event.event_id}
                event={event}
                showCustomer={false}
                onAcknowledge={() => {
                  feed.acknowledge(event.event_id);
                }}
                onDismiss={() => {
                  feed.dismiss(event.event_id);
                }}
              />
            ))}
        </ol>
      </div>
    </ModuleState>
  );
}

function InflowCard({
  event,
  showCustomer,
  onOpen,
  onAcknowledge,
  onDismiss,
}: {
  readonly event: MoneyInMotionEvent;
  readonly showCustomer: boolean;
  readonly onOpen?: () => void;
  readonly onAcknowledge: () => void;
  readonly onDismiss: () => void;
}): React.JSX.Element {
  const urgency = urgencyOf(event.days_remaining);
  const days = event.days_remaining;

  return (
    <li className={`inflow inflow--${urgency}`}>
      <div className="inflow__head">
        <span className={`inflow__urgency inflow__urgency--${urgency}`}>
          <span className="inflow__urgency-glyph" aria-hidden="true">
            {urgency === 'today' ? '⏰' : urgency === 'soon' ? '⏳' : '🗓'}
          </span>
          {URGENCY_LABEL[urgency]}
        </span>
        <span className="inflow__amount">{formatCents(event.amount_cents)}</span>
      </div>

      <p className="inflow__source">
        {INFLOW_SOURCE_LABEL[event.inferred_source]}
        <span className="inflow__confidence"> · {formatConfidence(event.confidence)}</span>
      </p>

      {/* The window meter: decorative reinforcement of the day count stated in words below it. */}
      <div className="inflow__window">
        <span className="inflow__window-track" aria-hidden="true">
          <span
            className={`inflow__window-fill inflow__window-fill--${urgency}`}
            style={{ width: `${String(windowPct(days))}%` }}
          />
        </span>
        <span className="inflow__window-label">
          {days <= 0
            ? 'Window closing today'
            : `${String(days)} day${days === 1 ? '' : 's'} left in the window`}
        </span>
      </div>

      <p className="inflow__summary">{event.evidence.summary}</p>

      <p className="inflow__action">
        <span className="inflow__action-tag" aria-hidden="true">
          ➜
        </span>
        <span>
          <span className="sr-only">Recommended action: </span>
          {event.recommended_action}
        </span>
      </p>

      <div className="inflow__chips">
        {showCustomer && onOpen !== undefined && (
          <button type="button" className="inflow__customer-link focus-ring" onClick={onOpen}>
            {labelFor(event)}
          </button>
        )}
        {Object.entries(event.evidence.details ?? {}).map(([key, value]) => (
          <span key={key} className="chip chip--muted">
            {key}: {value}
          </span>
        ))}
      </div>

      <div className="inflow__actions">
        {onOpen !== undefined && (
          <button type="button" className="button" onClick={onOpen}>
            Open 360
          </button>
        )}
        <button type="button" className="button button--primary" onClick={onAcknowledge}>
          Log outreach
        </button>
        <button type="button" className="button" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </li>
  );
}

/**
 * How much of the action window remains, as a percentage of a nominal 14-day window. Presentational
 * only — the day count is the accessible value.
 */
function windowPct(daysRemaining: number): number {
  const clamped = Math.max(0, Math.min(14, daysRemaining));
  return Math.max(4, (clamped / 14) * 100);
}

/**
 * The customer button label. A masked customer name arrives as a partial string (`"R. Alvarez"`,
 * `"Renata ***"`); when the name is hidden entirely the id is the label.
 */
function labelFor(event: MoneyInMotionEvent): string {
  const label = event.customer_label;
  if (typeof label === 'string' && label !== '') {
    return `${label} · ${event.customer_id}`;
  }
  return `Customer ${event.customer_id}`;
}
