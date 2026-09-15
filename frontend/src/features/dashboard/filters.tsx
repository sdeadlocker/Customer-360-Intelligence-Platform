import { createContext, useContext, useMemo, useReducer } from 'react';

import type { CustomerSegment } from '../../api/types';

/**
 * Dashboard filter state (task 12.4, requirement 3.7).
 *
 * The header owns a small set of filters — a date range, a set of customer segments, and a set of
 * product types — that time- and product-scoped widgets read. Holding them in context means every
 * widget below the header reacts to a filter change without the header threading props down through
 * the tree, and it is the single place the "active filter set is always visible" summary reads from.
 *
 * The filters are the *client's* current view intent; the entitlement scope that decides what a user
 * may see at all is the server's job (design §7.2) and is never expressed here. A widget that ignores
 * a filter (a profile card has no time dimension) simply does not read it.
 */

export type ProductType = 'DEPOSIT' | 'LOAN' | 'CARD' | 'INVESTMENT';

export interface DateRange {
  /** ISO-8601 date (YYYY-MM-DD), inclusive. `null` means unbounded on that side. */
  readonly from: string | null;
  readonly to: string | null;
}

export interface DashboardFilters {
  readonly dateRange: DateRange;
  readonly segments: readonly CustomerSegment[];
  readonly products: readonly ProductType[];
}

export const EMPTY_FILTERS: DashboardFilters = {
  dateRange: { from: null, to: null },
  segments: [],
  products: [],
};

type FilterAction =
  | { readonly type: 'setDateRange'; readonly range: DateRange }
  | { readonly type: 'toggleSegment'; readonly segment: CustomerSegment }
  | { readonly type: 'toggleProduct'; readonly product: ProductType }
  | { readonly type: 'clearAll' };

function toggle<T>(items: readonly T[], value: T): T[] {
  return items.includes(value) ? items.filter((item) => item !== value) : [...items, value];
}

export function filterReducer(state: DashboardFilters, action: FilterAction): DashboardFilters {
  switch (action.type) {
    case 'setDateRange':
      return { ...state, dateRange: action.range };
    case 'toggleSegment':
      return { ...state, segments: toggle(state.segments, action.segment) };
    case 'toggleProduct':
      return { ...state, products: toggle(state.products, action.product) };
    case 'clearAll':
      return EMPTY_FILTERS;
    default:
      return state;
  }
}

/** Whether any filter is set — drives whether the "active filters" summary and Clear appear. */
export function hasActiveFilters(filters: DashboardFilters): boolean {
  return (
    filters.dateRange.from !== null ||
    filters.dateRange.to !== null ||
    filters.segments.length > 0 ||
    filters.products.length > 0
  );
}

interface FilterContextValue {
  readonly filters: DashboardFilters;
  readonly dispatch: React.Dispatch<FilterAction>;
}

const FilterContext = createContext<FilterContextValue | null>(null);

export function FilterProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [filters, dispatch] = useReducer(filterReducer, EMPTY_FILTERS);
  const value = useMemo(() => ({ filters, dispatch }), [filters]);
  return <FilterContext.Provider value={value}>{children}</FilterContext.Provider>;
}

/** Read the current filters (and dispatch). Time- and product-scoped widgets call this. */
export function useFilters(): FilterContextValue {
  const value = useContext(FilterContext);
  if (value === null) {
    throw new Error('useFilters must be used within a FilterProvider');
  }
  return value;
}
