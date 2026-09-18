import { formatCents, type Maskable } from '../dashboard/widgets/format';
import type { ExternalHoldingType, InflowSource, LeakType, RevenuePlay } from './revenueApi';

/**
 * Shared vocabulary and chrome for the revenue plays (Phase 22).
 *
 * Every machine token the revenue endpoints emit is mapped to a human label here rather than
 * humanized generically, because these read better as domain phrases than as sentence-cased enums
 * ("Held-away capture", not "Held away capture"). Requirement 16.1c asks for the human form; this is
 * where the revenue feature satisfies it.
 */

export const PLAY_LABEL: Record<RevenuePlay, string> = {
  FEE_RECOVERY: 'Fee recovery',
  MONEY_IN_MOTION: 'Money in motion',
  HELD_AWAY_CAPTURE: 'Held-away capture',
  DEPOSIT_RETENTION: 'Deposit retention',
};

/** A decorative glyph per play. Always rendered inside an `aria-hidden` span beside the label. */
export const PLAY_GLYPH: Record<RevenuePlay, string> = {
  FEE_RECOVERY: '🧾',
  MONEY_IN_MOTION: '💸',
  HELD_AWAY_CAPTURE: '🏦',
  DEPOSIT_RETENTION: '🔒',
};

export const INFLOW_SOURCE_LABEL: Record<InflowSource, string> = {
  PROPERTY_SALE: 'Property sale',
  BONUS: 'Bonus',
  INHERITANCE: 'Inheritance',
  BUSINESS_EXIT: 'Business exit',
  INSURANCE_SETTLEMENT: 'Insurance settlement',
  UNCLASSIFIED: 'Unclassified inflow',
};

export const HOLDING_TYPE_LABEL: Record<ExternalHoldingType, string> = {
  BROKERAGE: 'Investments held away',
  MORTGAGE: 'External mortgage',
  CREDIT_CARD: 'External card balance',
  AUTO_LOAN: 'External auto loan',
  PAYROLL: 'Payroll at another bank',
  INSURANCE: 'External insurance',
};

export const LEAK_TYPE_LABEL: Record<LeakType, string> = {
  FEE_WAIVED: 'Waiver never restored',
  LEGACY_PRICING: 'Legacy pricing',
  UNBILLED_SERVICE: 'Unbilled service',
  MISSED_MINIMUM: 'Minimum not charged',
};

/**
 * The urgency tier of a money-in-motion window, stated in words. Urgency is never colour-only
 * (requirement 16.1): the tier label and the day count are both text.
 */
export type Urgency = 'today' | 'soon' | 'open';

export function urgencyOf(daysRemaining: number): Urgency {
  if (daysRemaining <= 1) {
    return 'today';
  }
  return daysRemaining <= 4 ? 'soon' : 'open';
}

export const URGENCY_LABEL: Record<Urgency, string> = {
  today: 'Act today',
  soon: 'Closing soon',
  open: 'Window open',
};

/**
 * A visible marker that the figures on screen are an illustrative layout preview, not grounded data.
 *
 * Rendered whenever a revenue hook falls back because the endpoint is absent. It is deliberately
 * loud and it is real text (not a tooltip or a colour), so a preview can never be mistaken for a
 * customer's actual position.
 */
export function PreviewNote({ what }: { readonly what: string }): React.JSX.Element {
  return (
    <p className="revenue-preview" role="note">
      <span className="badge badge--degraded">Preview</span>{' '}
      <span>
        Illustrative figures — the {what} engine has not run. These numbers are a layout preview and
        are not this customer&apos;s data.
      </span>
    </p>
  );
}

/** The Preview badge on its own, for a card head beside the title. */
export function PreviewBadge(): React.JSX.Element {
  return (
    <span className="badge badge--degraded" title="Illustrative figures, not grounded data">
      Preview
    </span>
  );
}

/**
 * Format a basis-point share as a whole percentage for display (`2704` → `27%`). Null when the
 * server could not compute it because an input was masked.
 */
export function formatShare(bps: number | null): string {
  if (bps === null) {
    return '—';
  }
  return `${String(Math.round(bps / 100))}%`;
}

/**
 * A compact money label for a dense tile (`$1.9M`, `$310K`, `$2,568`).
 *
 * A masked band string passes straight through, exactly as {@link formatCents} does — the server
 * already chose the vocabulary and re-formatting it would corrupt it.
 */
export function formatCompactCents(value: Maskable): string {
  if (value === null || value === undefined) {
    return '—';
  }
  if (typeof value === 'string') {
    return value;
  }
  const dollars = value / 100;
  const abs = Math.abs(dollars);
  if (abs >= 1_000_000) {
    return `${signOf(dollars)}$${(abs / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)}M`;
  }
  if (abs >= 1_000) {
    return `${signOf(dollars)}$${(abs / 1_000).toFixed(abs >= 100_000 ? 0 : 1)}K`;
  }
  return formatCents(value);
}

function signOf(value: number): string {
  return value < 0 ? '−' : '';
}

/** Render a 0–1 confidence as a labelled percentage, never a bare number. */
export function formatConfidence(confidence: number): string {
  return `${String(Math.round(confidence * 100))}% confidence`;
}
