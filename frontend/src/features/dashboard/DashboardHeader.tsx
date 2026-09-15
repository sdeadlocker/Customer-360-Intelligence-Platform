import { useId, useState } from 'react';

import type { CustomerSegment } from '../../api/types';
import { useAuth } from '../../auth/AuthContext';
import { ThemeToggle } from '../../theme/ThemeToggle';
import type { Dashboard360 } from './dashboardData';
import { ExportMenu } from './ExportMenu';
import { type ProductType, useFilters } from './filters';

/**
 * The dashboard header and filter bar (task 12.4, requirement 3.7).
 *
 * It carries: who is signed in and a sign-out control; a customer selector (a "change customer"
 * action that returns to search); a date range and segment/product filters; and — always visible
 * whenever anything is set — the active filter summary, so a user is never surprised by data that is
 * silently scoped. Every control writes to the shared filter context, which the time- and
 * product-scoped widgets read (requirement 3.7).
 */

const SEGMENTS: readonly CustomerSegment[] = ['MASS', 'AFFLUENT', 'HNW', 'UHNW', 'SMALL_BUSINESS'];
const PRODUCTS: readonly ProductType[] = ['DEPOSIT', 'LOAN', 'CARD', 'INVESTMENT'];

/** A small decorative glyph per product/segment chip (presentational only). */
const PRODUCT_ICON: Record<ProductType, string> = {
  DEPOSIT: '🏦',
  LOAN: '📉',
  CARD: '💳',
  INVESTMENT: '📈',
};

const SEGMENT_ICON: Record<CustomerSegment, string> = {
  MASS: '👥',
  AFFLUENT: '💠',
  HNW: '💎',
  UHNW: '👑',
  SMALL_BUSINESS: '🏢',
};

export function DashboardHeader({
  customerId,
  onChangeCustomer,
  view,
}: {
  readonly customerId: string;
  readonly onChangeCustomer: () => void;
  /** The loaded 360 payload, present once the dashboard has data — enables the export control. */
  readonly view?: Dashboard360 | undefined;
}): React.JSX.Element {
  const { principal, logout } = useAuth();
  const { filters, dispatch } = useFilters();
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersPanelId = useId();

  const activeCount =
    (filters.dateRange.from !== null || filters.dateRange.to !== null ? 1 : 0) +
    filters.segments.length +
    filters.products.length;

  return (
    <header className="dash-header">
      <div className="dash-header__top">
        {/* Left zone: just the brand. */}
        <div className="dash-header__identity">
          <span className="dash-header__brand">Customer 360</span>
        </div>

        {/* Right zone: the current customer + Change customer, a divider, then date, theme, export,
            account. */}
        <div className="dash-header__actions">
          <div className="dash-header__group">
            <span className="dash-header__customer mono" aria-label="Current customer">
              {customerId}
            </span>
            <button type="button" className="button" onClick={onChangeCustomer}>
              Change customer
            </button>
          </div>

          <span className="dash-header__divider" aria-hidden="true" />

          <div className="dash-header__group dash-header__group--utility">
            <TodayChip />
            <ThemeToggle />
            {view !== undefined && <ExportMenu view={view} />}
            <span className="dash-header__avatar" aria-hidden="true">
              👤
            </span>
            <button type="button" className="button button--subtle" onClick={logout}>
              Sign out
            </button>
          </div>
        </div>
      </div>

      {/* A compact filter toolbar: a toggle that expands the three filter groups on demand, so the
          filters no longer occupy a full band at the top of every dashboard. When filters are set,
          a live summary and a clear-all sit inline, so scoping is never hidden even when collapsed. */}
      <div className="dash-filterbar">
        {principal !== null && (
          <span
            className="dash-header__signedin"
            title={`Signed in as ${principal.user_id} (${principal.entitlement.kind})`}
          >
            <span className="dash-header__signedin-avatar" aria-hidden="true">
              👤
            </span>
            <span className="dash-header__signedin-text">
              <span className="dash-header__signedin-label">Signed in as</span>
              <span className="dash-header__signedin-role">
                {principal.role} · {principal.entitlement.kind}
              </span>
            </span>
          </span>
        )}
        <button
          type="button"
          className={`dash-filterbar__toggle focus-ring${filtersOpen ? ' dash-filterbar__toggle--open' : ''}`}
          aria-expanded={filtersOpen}
          aria-controls={filtersPanelId}
          onClick={() => {
            setFiltersOpen((v) => !v);
          }}
        >
          <span aria-hidden="true">🎛️</span>
          Filters
          {activeCount > 0 && (
            <span className="dash-filterbar__count" aria-label={`${activeCount} active`}>
              {activeCount}
            </span>
          )}
          <span className="dash-filterbar__chevron" aria-hidden="true">
            {filtersOpen ? '▲' : '▼'}
          </span>
        </button>

        {activeCount > 0 && (
          <div className="dash-filterbar__summary" role="status" aria-live="polite">
            <ActiveFilterSummary />
            <button
              type="button"
              className="button button--subtle"
              onClick={() => {
                dispatch({ type: 'clearAll' });
              }}
            >
              Clear all
            </button>
          </div>
        )}
      </div>

      {filtersOpen && (
        <div
          id={filtersPanelId}
          className="dash-header__filters"
          role="group"
          aria-label="Dashboard filters"
        >
          <fieldset className="filter-group filter-group--date">
            <legend data-icon="📅">Date range</legend>
            <label className="filter-date">
              <span>From</span>
              <input
                type="date"
                value={filters.dateRange.from ?? ''}
                onChange={(event) => {
                  dispatch({
                    type: 'setDateRange',
                    range: { ...filters.dateRange, from: event.target.value || null },
                  });
                }}
              />
            </label>
            <label className="filter-date">
              <span>To</span>
              <input
                type="date"
                value={filters.dateRange.to ?? ''}
                onChange={(event) => {
                  dispatch({
                    type: 'setDateRange',
                    range: { ...filters.dateRange, to: event.target.value || null },
                  });
                }}
              />
            </label>
          </fieldset>

          <fieldset className="filter-group filter-group--segments">
            <legend data-icon="🏷️">Segments</legend>
            <div className="filter-chips">
              {SEGMENTS.map((segment) => (
                <ToggleChip
                  key={segment}
                  label={segment}
                  icon={SEGMENT_ICON[segment]}
                  active={filters.segments.includes(segment)}
                  onToggle={() => {
                    dispatch({ type: 'toggleSegment', segment });
                  }}
                />
              ))}
            </div>
          </fieldset>

          <fieldset className="filter-group filter-group--products">
            <legend data-icon="🛍️">Products</legend>
            <div className="filter-chips">
              {PRODUCTS.map((product) => (
                <ToggleChip
                  key={product}
                  label={product}
                  icon={PRODUCT_ICON[product]}
                  active={filters.products.includes(product)}
                  onToggle={() => {
                    dispatch({ type: 'toggleProduct', product });
                  }}
                />
              ))}
            </div>
          </fieldset>
        </div>
      )}
    </header>
  );
}

/** Today's date as a compact chip (day number + weekday/month), like the reference dashboard. */
function TodayChip(): React.JSX.Element {
  const now = new Date();
  const day = now.toLocaleDateString('en-US', { day: 'numeric' });
  const weekday = now.toLocaleDateString('en-US', { weekday: 'short' });
  const month = now.toLocaleDateString('en-US', { month: 'long' });
  const full = now.toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  });
  return (
    <span className="today-chip" title={full}>
      <span className="today-chip__day" aria-hidden="true">
        {day}
      </span>
      <span className="today-chip__text" aria-hidden="true">
        <span className="today-chip__weekday">{weekday},</span>
        <span className="today-chip__month">{month}</span>
      </span>
      <span className="sr-only">Today is {full}</span>
    </span>
  );
}

function ActiveFilterSummary(): React.JSX.Element {
  const { filters } = useFilters();
  const chips: string[] = [];
  if (filters.dateRange.from !== null || filters.dateRange.to !== null) {
    chips.push(`${filters.dateRange.from ?? '…'} → ${filters.dateRange.to ?? '…'}`);
  }
  for (const segment of filters.segments) {
    chips.push(segment);
  }
  for (const product of filters.products) {
    chips.push(product);
  }
  return (
    <span className="active-chips">
      {chips.map((chip) => (
        <span key={chip} className="badge badge--active">
          {chip}
        </span>
      ))}
    </span>
  );
}

function ToggleChip({
  label,
  active,
  onToggle,
  icon,
}: {
  readonly label: string;
  readonly active: boolean;
  readonly onToggle: () => void;
  readonly icon?: string;
}): React.JSX.Element {
  return (
    <button
      type="button"
      className={`chip filter-chip ${active ? 'chip--active' : ''}`}
      aria-pressed={active}
      onClick={onToggle}
    >
      {icon !== undefined && (
        <span className="filter-chip__icon" aria-hidden="true">
          {icon}
        </span>
      )}
      {label}
    </button>
  );
}
