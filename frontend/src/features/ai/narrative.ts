/**
 * Humanize the machine-shaped fact fragments a template (degraded) narrative is built from.
 *
 * When the AI model is unavailable the backend falls back to a deterministic template renderer
 * (`agents/templates.py`) that emits one fact per line in the exact machine form
 *
 *   `<entity_type>.<field> is <value> [<fact_id>]`
 *
 * e.g. `customer.customer_name is Marta Farooqi [F1]` or
 * `financial_profile.net_worth_cents is 100000 [F5]`. Rendered verbatim that reads as raw field
 * paths, unformatted cents and stray `[F1]` markers. This module turns one such line into a
 * readable label / value / citation triple the card can lay out cleanly, while leaving genuine
 * prose (a real model's narrative) untouched.
 *
 * The value formatting mirrors the dashboard widgets' rules: a `*_cents` field is integer cents
 * rendered as USD; a `SCREAMING_SNAKE` band is humanized; a server-computed mask string
 * (`"$1K-$10K"`, `"Renata ***"`) passes through unchanged.
 */

const CURRENCY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

/** A parsed template fact line. */
export interface FactLine {
  /** A human label for the field, e.g. "Net worth". */
  readonly label: string;
  /** The display-ready value, e.g. "$1,000" or "Mass". */
  readonly value: string;
  /** The citation marker without brackets, e.g. "F1", or null when the line carried none. */
  readonly citation: string | null;
}

/** `entity_type.field is value [F1]` — the citation is optional. */
const FACT_LINE_RE = /^([a-z_]+)\.([a-z0-9_]+)\s+is\s+(.+?)(?:\s+\[([FP]\d+)\])?$/i;

/** Does a string look like a raw multi-word enum token (`VERY_LOW`) rather than display text? */
function looksLikeEnumToken(value: string): boolean {
  return value.includes('_') && /^[A-Z0-9_]+$/.test(value) && /[A-Z]/.test(value);
}

/** Turn `NET_WORTH` / `net worth` into "Net worth". */
function humanizeToken(value: string): string {
  const lowered = value.replace(/_/g, ' ').trim().toLowerCase();
  return lowered.charAt(0).toUpperCase() + lowered.slice(1);
}

/**
 * A readable label for an `entity.field` pair. The field alone is almost always enough
 * ("net_worth_cents" → "Net worth"); the `_cents` suffix is dropped because the value is rendered
 * as currency, and a trailing `_bps` likewise.
 */
function labelFor(field: string): string {
  const base = field.replace(/_(cents|bps)$/i, '');
  return humanizeToken(base);
}

/** Is this field a monetary amount carried as integer cents? */
function isCentsField(field: string): boolean {
  return /_cents$/i.test(field);
}

/** Format a raw fact value for display, given the field it belongs to. */
function formatValue(field: string, raw: string): string {
  const value = raw.trim();
  // A monetary cents field: format an integer as USD, but pass a server band string through.
  if (isCentsField(field)) {
    if (/^-?\d+$/.test(value)) {
      return CURRENCY.format(Number(value) / 100);
    }
    return value; // already a masked band like "$1K-$10K"
  }
  // A bare enum band (MASS, SILVER, VERY_LOW) reads as a machine token: humanize it. A single
  // all-caps word (SILVER) or a multi-word token (VERY_LOW) both qualify; mixed/normal text does not.
  if (
    /^[A-Z0-9_]+$/.test(value) &&
    /[A-Z]/.test(value) &&
    (looksLikeEnumToken(value) || /^[A-Z]+$/.test(value))
  ) {
    return humanizeToken(value);
  }
  return value;
}

/**
 * Humanize an offer rationale (`PRODUCT_AFFINITY:SAVINGS; high confidence`) into readable text,
 * dropping the confidence tail (surfaced elsewhere) and humanizing the `TYPE:SUBJECT` basis.
 */
export function humanizeRationale(rationale: string): string {
  const withoutConfidence = rationale
    .replace(/;?\s*\b(low|medium|high)\s+confidence\b/i, '')
    .replace(/;+\s*$/, '')
    .trim();
  if (withoutConfidence === '') {
    return 'Ranked by expected value';
  }
  const colon = withoutConfidence.indexOf(':');
  if (colon === -1) {
    return looksLikeEnumToken(withoutConfidence) || /^[A-Z]+$/.test(withoutConfidence)
      ? humanizeToken(withoutConfidence)
      : withoutConfidence;
  }
  const type = humanizeToken(withoutConfidence.slice(0, colon).trim());
  const subject = withoutConfidence.slice(colon + 1).trim();
  return subject === '' ? type : `${type} · ${humanizeToken(subject)}`;
}

/**
 * Humanize a suppression reason's leading enum token (`COOLING_OFF: declined …` → `Cooling off:
 * declined …`, `DUPLICATE_PRODUCT` → `Duplicate product`), keeping any human detail after the colon.
 */
export function humanizeReason(reason: string): string {
  const colon = reason.indexOf(':');
  if (colon === -1) {
    return /^[A-Z0-9_]+$/.test(reason) ? humanizeToken(reason) : reason;
  }
  const token = reason.slice(0, colon);
  const detail = reason.slice(colon + 1).trim();
  const head = /^[A-Z0-9_]+$/.test(token) ? humanizeToken(token) : token;
  return detail === '' ? head : `${head}: ${detail}`;
}

/**
 * Parse one template fact line into a readable {@link FactLine}, or null if it is not a fact line
 * (in which case the caller should render the text as prose).
 */
export function parseFactLine(line: string): FactLine | null {
  const match = FACT_LINE_RE.exec(line.trim());
  if (match === null) {
    return null;
  }
  const [, , field, rawValue, citation] = match;
  return {
    label: labelFor(field ?? ''),
    value: formatValue(field ?? '', rawValue ?? ''),
    citation: citation ?? null,
  };
}
