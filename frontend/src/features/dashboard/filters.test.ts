import { describe, expect, it } from 'vitest';

import { EMPTY_FILTERS, filterReducer, hasActiveFilters } from './filters';

describe('filterReducer', () => {
  it('toggles a segment on and off', () => {
    const once = filterReducer(EMPTY_FILTERS, { type: 'toggleSegment', segment: 'HNW' });
    expect(once.segments).toEqual(['HNW']);
    const twice = filterReducer(once, { type: 'toggleSegment', segment: 'HNW' });
    expect(twice.segments).toEqual([]);
  });

  it('toggles a product independently of segments', () => {
    const next = filterReducer(EMPTY_FILTERS, { type: 'toggleProduct', product: 'LOAN' });
    expect(next.products).toEqual(['LOAN']);
    expect(next.segments).toEqual([]);
  });

  it('sets a date range', () => {
    const next = filterReducer(EMPTY_FILTERS, {
      type: 'setDateRange',
      range: { from: '2025-01-01', to: '2025-12-31' },
    });
    expect(next.dateRange).toEqual({ from: '2025-01-01', to: '2025-12-31' });
  });

  it('clears everything', () => {
    const dirty = filterReducer(
      filterReducer(EMPTY_FILTERS, { type: 'toggleSegment', segment: 'MASS' }),
      { type: 'toggleProduct', product: 'CARD' },
    );
    expect(hasActiveFilters(dirty)).toBe(true);
    const cleared = filterReducer(dirty, { type: 'clearAll' });
    expect(cleared).toEqual(EMPTY_FILTERS);
    expect(hasActiveFilters(cleared)).toBe(false);
  });
});

describe('hasActiveFilters', () => {
  it('is false for the empty filter set', () => {
    expect(hasActiveFilters(EMPTY_FILTERS)).toBe(false);
  });

  it('is true when only one side of the date range is set', () => {
    expect(
      hasActiveFilters({ ...EMPTY_FILTERS, dateRange: { from: '2025-01-01', to: null } }),
    ).toBe(true);
  });
});
