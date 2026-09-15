/**
 * Shared formatting helpers for the Phase 13 dashboard widgets.
 *
 * Two rules from the platform's contract shape everything here:
 *
 * 1. Money is integer cents on the wire and only becomes a decimal at the display boundary (design
 *    §1.1). A widget never does arithmetic on a formatted string; it formats a cents integer once,
 *    at the leaf, with {@link formatCents}.
 *
 * 2. A maskable field arrives as one of three things depending on the caller's role
 *    (security/serializer.py): the real value (a number for a `*_cents` field), a *band or partial
 *    string* the server already computed (`"$100K-$1M"`, `"GOOD"`, `"Renata ***"`), or — when the
 *    field is HIDDEN — it is simply absent. So every maskable value is typed `number | string |
 *    null | undefined`, and the formatter passes a server-supplied string straight through rather
 *    than trying to re-format it. The widget must not assume a number.
 */

/** A value that may have been masked to a band/partial string, or hidden entirely. */
export type Maskable = number | string | null | undefined;

const CURRENCY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

const CURRENCY_WITH_CENTS = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/**
 * Format a monetary value for display.
 *
 * When `value` is a number it is treated as integer cents and rendered as USD. When it is a string
 * the server has already masked it to a band label (`"$100K-$1M"`) or partial, and it is returned
 * unchanged — re-formatting it would corrupt it. When it is absent the field was hidden, and a
 * caller-supplied `hiddenLabel` (default `"—"`) is shown.
 */
export function formatCents(
  value: Maskable,
  opts: { withCents?: boolean; hiddenLabel?: string } = {},
): string {
  const { withCents = false, hiddenLabel = '—' } = opts;
  if (value === null || value === undefined) {
    return hiddenLabel;
  }
  if (typeof value === 'string') {
    return value;
  }
  const dollars = value / 100;
  return withCents ? CURRENCY_WITH_CENTS.format(dollars) : CURRENCY.format(dollars);
}

/** Format basis points (1/100 of a percent) as a percentage, or pass a masked string through. */
export function formatBps(value: Maskable, opts: { hiddenLabel?: string } = {}): string {
  const { hiddenLabel = '—' } = opts;
  if (value === null || value === undefined) {
    return hiddenLabel;
  }
  if (typeof value === 'string') {
    return value;
  }
  return `${(value / 100).toFixed(2)}%`;
}

/** Format a raw number (a score, a count), or pass a masked band string through. */
export function formatNumber(value: Maskable, opts: { hiddenLabel?: string } = {}): string {
  const { hiddenLabel = '—' } = opts;
  if (value === null || value === undefined) {
    return hiddenLabel;
  }
  if (typeof value === 'string') {
    return value;
  }
  return new Intl.NumberFormat('en-US').format(value);
}

/**
 * Format an ISO date (`YYYY-MM-DD`) or datetime for display, medium style.
 *
 * A partial-masked date arrives as a year-only string (`"1982"`); that is not a parseable ISO date
 * and is returned as-is. A `null`/`undefined` value renders the hidden label.
 */
export function formatDate(value: string | null | undefined, hiddenLabel = '—'): string {
  if (value === null || value === undefined || value === '') {
    return hiddenLabel;
  }
  // A year-only mask (`"1982"`) is shown verbatim — it is not a full date, even though `Date` would
  // happily parse it as the first of January. Only a full `YYYY-MM-DD` is formatted as a date.
  if (!/^\d{4}-\d{2}-\d{2}/.test(value)) {
    return value;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

/** Format a `YYYY-MM` month key as `Mon YYYY`, or return it unchanged if not that shape. */
export function formatMonth(month: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(month);
  if (match === null) {
    return month;
  }
  const [, year, mm] = match;
  const parsed = new Date(Number(year), Number(mm) - 1, 1);
  return parsed.toLocaleDateString('en-US', { year: 'numeric', month: 'short' });
}

/** Turn an ENUM_VALUE into "Enum value" for display without losing meaning. */
export function humanizeEnum(value: string | null | undefined, hiddenLabel = '—'): string {
  if (value === null || value === undefined || value === '') {
    return hiddenLabel;
  }
  const lowered = value.replace(/_/g, ' ').toLowerCase();
  return lowered.charAt(0).toUpperCase() + lowered.slice(1);
}

/**
 * Does a string look like a raw enum token — `VERY_LOW`, `COOLING_OFF`, `HIGH` — rather than a
 * human phrase or a server-computed mask string like `"$100K-$1M"`, `"Renata ***"` or `"GOOD"`?
 *
 * The test is intentionally strict: upper-case letters, digits and underscores only, with at least
 * one letter. A single all-caps word (`GOOD`, `HIGH`) is a band the server states in its own
 * vocabulary and is left alone; a *multi-word* token (`VERY_LOW`) is the machine form a reader
 * should never see, so {@link humanizeMasked} rewrites it. Anything with a space, lower-case letter
 * or punctuation (`$`, `*`, `-`) is already display-ready and passes through untouched.
 */
function looksLikeEnumToken(value: string): boolean {
  return value.includes('_') && /^[A-Z0-9_]+$/.test(value) && /[A-Z]/.test(value);
}

/**
 * Render a maskable value that might be a raw multi-word enum band (`VERY_LOW`) as a human phrase,
 * while passing every already-display-ready string through unchanged.
 *
 * A maskable field arrives as a number, a server-supplied mask string (a band like `"GOOD"`, a
 * range like `"$100K-$1M"`, a partial like `"Renata ***"`) or nothing (hidden). The only case that
 * reads badly on screen is a multi-word `SCREAMING_SNAKE` band such as `VERY_LOW` / `VERY_HIGH`
 * (from `security/masking.py`); this humanizes exactly those and leaves the rest intact, so it is
 * safe to use anywhere a masked value is shown as text.
 */
export function humanizeMasked(value: Maskable, hiddenLabel = '—'): string {
  if (value === null || value === undefined || value === '') {
    return hiddenLabel;
  }
  if (typeof value === 'number') {
    return String(value);
  }
  return looksLikeEnumToken(value) ? humanizeEnum(value) : value;
}

/** The as-of provenance line every widget shows, from a domain row's `as_of_date`/`source_system`. */
export function provenance(
  asOf: string | null | undefined,
  source: string | null | undefined,
): string {
  const parts: string[] = [];
  if (asOf !== null && asOf !== undefined && asOf !== '') {
    parts.push(`As of ${formatDate(asOf)}`);
  }
  if (source !== null && source !== undefined && source !== '') {
    parts.push(source);
  }
  return parts.join(' · ');
}
