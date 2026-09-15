import { describe, expect, it } from 'vitest';

import {
  formatBps,
  formatCents,
  formatDate,
  formatMonth,
  humanizeEnum,
  provenance,
} from './format';

describe('formatCents', () => {
  it('formats integer cents as whole dollars by default', () => {
    expect(formatCents(510000000)).toBe('$5,100,000');
  });

  it('formats with cents when asked', () => {
    expect(formatCents(12345, { withCents: true })).toBe('$123.45');
  });

  it('passes a server-supplied band string through unchanged', () => {
    expect(formatCents('$100K-$1M')).toBe('$100K-$1M');
  });

  it('renders the hidden label when the value is absent', () => {
    expect(formatCents(undefined)).toBe('—');
    expect(formatCents(null)).toBe('—');
  });
});

describe('formatBps', () => {
  it('renders basis points as a percentage', () => {
    expect(formatBps(450)).toBe('4.50%');
  });

  it('passes a masked string through', () => {
    expect(formatBps('MEDIUM')).toBe('MEDIUM');
  });
});

describe('formatDate', () => {
  it('formats an ISO date medium', () => {
    expect(formatDate('2015-03-01')).toMatch(/Mar 1, 2015/);
  });

  it('returns a year-only mask verbatim', () => {
    expect(formatDate('1982')).toBe('1982');
  });

  it('renders the hidden label for an absent value', () => {
    expect(formatDate(undefined)).toBe('—');
  });
});

describe('formatMonth', () => {
  it('renders a YYYY-MM key as a month and year', () => {
    expect(formatMonth('2026-01')).toMatch(/Jan 2026/);
  });
});

describe('humanizeEnum', () => {
  it('turns an ENUM_VALUE into a sentence-cased phrase', () => {
    expect(humanizeEnum('CROSS_SELL')).toBe('Cross sell');
  });

  it('renders the hidden label for an absent value', () => {
    expect(humanizeEnum(null)).toBe('—');
  });
});

describe('provenance', () => {
  it('joins as-of and source', () => {
    expect(provenance('2026-09-01', 'CORE')).toMatch(/^As of Sep 1, 2026 · CORE$/);
  });

  it('omits an absent source', () => {
    expect(provenance('2026-09-01', null)).toMatch(/^As of Sep 1, 2026$/);
  });
});
