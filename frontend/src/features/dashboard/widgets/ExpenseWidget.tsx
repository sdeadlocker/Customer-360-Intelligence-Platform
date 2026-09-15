import { useMemo } from 'react';

import { ModuleState } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';
import { useFilters } from '../filters';
import { ChartWithTable } from './ChartWithTable';
import { formatCents, formatMonth, humanizeEnum } from './format';
import { CompositionDonut, SmoothLineChart, type DonutSlice } from './MiniCharts';
import {
  pick,
  type CategoryTotalData,
  type ExpenseAnalyticsData,
  type MonthlyFlagData,
} from './types';
import { deriveWidgetState } from './widgetState';

/** A colour ramp for the category pie slices, matching the dashboard's fintech palette. */
const CATEGORY_COLORS = [
  '#6366f1',
  '#0ea5e9',
  '#14b8a6',
  '#f97316',
  '#ec4899',
  '#a855f7',
  '#22c55e',
  '#eab308',
] as const;

/**
 * Shared read of the expense analytics slice, honouring the dashboard date-range filter. Used by
 * the three expense cards (summary, category, trend) so each is an independent grid card while
 * reading the same masked, range-filtered data.
 */
function useExpense(view: Dashboard360): {
  state: ReturnType<typeof deriveWidgetState>;
  analytics: ExpenseAnalyticsData | undefined;
  categories: readonly CategoryTotalData[];
  monthly: readonly MonthlyFlagData[];
} {
  const { filters } = useFilters();
  const state = deriveWidgetState(view, { module: 'financial', slots: ['expense_analytics'] });
  const analytics = pick<ExpenseAnalyticsData>(view.raw, 'expense_analytics');
  const monthly = useMemo(() => {
    const series = analytics?.monthly ?? [];
    return series.filter((point) =>
      withinRange(point.month, filters.dateRange.from, filters.dateRange.to),
    );
  }, [analytics, filters.dateRange.from, filters.dateRange.to]);
  return { state, analytics, categories: analytics?.by_category ?? [], monthly };
}

/**
 * Expense analytics — summary card (task 13.3, requirements 5.8, 5.9, 16.2).
 *
 * The KPI headline tiles and the plain-language insight. The two charts (category distribution and
 * monthly trend) are their own dashboard cards ({@link ExpenseCategoryWidget},
 * {@link ExpenseTrendWidget}) so each visualization is a distinct, independently-sized card rather
 * than one long panel. All amounts are integer cents formatted once at the leaf.
 */
export function ExpenseWidget({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const { state, analytics, categories, monthly } = useExpense(view);
  const isEmpty =
    state.isEmpty || (categories.length === 0 && (analytics?.monthly ?? []).length === 0);
  const kpis = useMemo(() => summarise(categories, monthly), [categories, monthly]);

  return (
    <ModuleState title="Expense analytics" state={state.model} isEmpty={isEmpty}>
      {analytics !== undefined && (
        <div className="widget">
          <div className="expense-kpis">
            <ExpenseKpi
              icon="💸"
              tone="indigo"
              value={formatCents(kpis.total)}
              label="Total spend"
            />
            <ExpenseKpi
              icon="🏷️"
              tone="teal"
              value={kpis.topCategory !== null ? humanizeEnum(kpis.topCategory) : '—'}
              label="Top category"
            />
            <ExpenseKpi
              icon="📅"
              tone="violet"
              value={kpis.avgPerMonth !== null ? formatCents(kpis.avgPerMonth) : '—'}
              label="Avg / month"
            />
            <ExpenseKpi
              icon="🚩"
              tone={kpis.anomalies > 0 ? 'amber' : 'green'}
              value={String(kpis.anomalies)}
              label="Months flagged"
            />
          </div>
        </div>
      )}
    </ModuleState>
  );
}

/** Expense — spend-by-category card: the share donut beside the ranked bars (+ data table). */
export function ExpenseCategoryWidget({
  view,
}: {
  readonly view: Dashboard360;
}): React.JSX.Element {
  const { state, analytics, categories } = useExpense(view);
  const isEmpty = state.isEmpty || categories.length === 0;
  return (
    <ModuleState title="Spend by category" state={state.model} isEmpty={isEmpty}>
      {analytics !== undefined && categories.length > 0 && (
        <div className="widget">
          {/* The category-share donut is the visual; the ranked-bar chart was removed to keep the
              card compact. The accessible data table (requirement 16.2) is still available via the
              toggle. */}
          <ChartWithTable
            label="Spend by category"
            chart={<CategoryPie categories={categories} />}
            table={<CategoryTable categories={categories} />}
          />
        </div>
      )}
    </ModuleState>
  );
}

/** Expense — monthly-trend card: the smooth line above the monthly bars (+ data table). */
export function ExpenseTrendWidget({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const { state, analytics, monthly } = useExpense(view);
  const isEmpty = state.isEmpty || (analytics?.monthly ?? []).length === 0;
  return (
    <ModuleState title="Monthly spend trend" state={state.model} isEmpty={isEmpty}>
      {analytics !== undefined && monthly.length > 0 && (
        <div className="widget">
          <MonthlyLine months={monthly} />
          <ChartWithTable
            label={`Monthly spend trend; months beyond ${analytics.threshold_sigma ?? 2}σ of the trailing baseline are flagged`}
            chart={<MonthlyBars months={monthly} />}
            table={<MonthlyTable months={monthly} threshold={analytics.threshold_sigma} />}
          />
        </div>
      )}
    </ModuleState>
  );
}

interface ExpenseSummary {
  readonly total: number;
  readonly topCategory: string | null;
  readonly avgPerMonth: number | null;
  readonly anomalies: number;
}

/** Summary KPIs derived from the category breakdown and the range-filtered monthly series. */
function summarise(
  categories: readonly CategoryTotalData[],
  monthly: readonly MonthlyFlagData[],
): ExpenseSummary {
  const total = categories.reduce((sum, c) => sum + Math.max(0, c.total_cents), 0);
  const top = [...categories].sort((a, b) => b.total_cents - a.total_cents)[0];
  const monthlyTotal = monthly.reduce((sum, m) => sum + Math.max(0, m.total_cents), 0);
  const avgPerMonth = monthly.length > 0 ? Math.round(monthlyTotal / monthly.length) : null;
  const anomalies = monthly.filter((m) => m.is_anomaly).length;
  return {
    total,
    topCategory: top?.transaction_category ?? null,
    avgPerMonth,
    anomalies,
  };
}

function ExpenseKpi({
  icon,
  tone,
  value,
  label,
}: {
  readonly icon: string;
  readonly tone: string;
  readonly value: string;
  readonly label: string;
}): React.JSX.Element {
  return (
    <div className={`expense-kpi expense-kpi--${tone}`}>
      <span className="expense-kpi__icon" aria-hidden="true">
        {icon}
      </span>
      <span className="expense-kpi__body">
        <span className="expense-kpi__value">{value}</span>
        <span className="expense-kpi__label">{label}</span>
      </span>
    </div>
  );
}

/**
 * A category-share donut. Decorative companion to the ranked bars and the accessible category table
 * (which carry the same numbers), so it is aria-hidden inside {@link CompositionDonut}.
 */
function CategoryPie({
  categories,
}: {
  readonly categories: readonly CategoryTotalData[];
}): React.JSX.Element | null {
  const sorted = [...categories]
    .filter((c) => c.total_cents > 0)
    .sort((a, b) => b.total_cents - a.total_cents);
  if (sorted.length === 0) {
    return null;
  }
  const total = sorted.reduce((sum, c) => sum + c.total_cents, 0);
  // Keep the legend readable: top 6 categories, the rest folded into "Other".
  const top = sorted.slice(0, 6);
  const rest = sorted.slice(6);
  const slices: DonutSlice[] = top.map((c, i) => ({
    label: humanizeEnum(c.transaction_category),
    value: c.total_cents,
    color: CATEGORY_COLORS[i % CATEGORY_COLORS.length] ?? '#6366f1',
  }));
  if (rest.length > 0) {
    slices.push({
      label: 'Other',
      value: rest.reduce((sum, c) => sum + c.total_cents, 0),
      color: '#94a3b8',
    });
  }
  return (
    <div className="expense-pie">
      <CompositionDonut slices={slices} centerLabel="Total" centerValue={formatCents(total)} />
    </div>
  );
}

/**
 * A smooth monthly-spend line with anomaly markers. Decorative companion to the monthly bars and
 * table below (which carry the exact figures and the deviation labels), so it is aria-hidden inside
 * {@link SmoothLineChart}.
 */
function MonthlyLine({
  months,
}: {
  readonly months: readonly MonthlyFlagData[];
}): React.JSX.Element | null {
  if (months.length < 2) {
    return null;
  }
  return (
    <div className="expense-line">
      <SmoothLineChart
        values={months.map((m) => m.total_cents)}
        labels={months.map((m) => m.month.slice(2))}
        flags={months.map((m) => m.is_anomaly)}
        colorId="expense"
      />
    </div>
  );
}

/** Whether a `YYYY-MM` month lies within an inclusive `YYYY-MM-DD` range (either side optional). */
function withinRange(month: string, from: string | null, to: string | null): boolean {
  if (from !== null && month < from.slice(0, 7)) {
    return false;
  }
  if (to !== null && month > to.slice(0, 7)) {
    return false;
  }
  return true;
}

function CategoryTable({
  categories,
}: {
  readonly categories: readonly CategoryTotalData[];
}): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Spend by category</caption>
      <thead>
        <tr>
          <th scope="col">Category</th>
          <th scope="col">Total</th>
          <th scope="col">Transactions</th>
        </tr>
      </thead>
      <tbody>
        {categories.map((cat) => (
          <tr key={cat.transaction_category}>
            <th scope="row">{humanizeEnum(cat.transaction_category)}</th>
            <td>{formatCents(cat.total_cents)}</td>
            <td>{cat.transaction_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MonthlyBars({
  months,
}: {
  readonly months: readonly MonthlyFlagData[];
}): React.JSX.Element {
  if (months.length === 0) {
    return <p className="module__empty">No spend in the selected range.</p>;
  }
  const max = Math.max(...months.map((m) => m.total_cents), 1);
  return (
    <ul className="trend">
      {months.map((point) => (
        <li
          key={point.month}
          className="trend__col"
          title={`${formatMonth(point.month)}: ${formatCents(point.total_cents)}`}
        >
          <span className="trend__bar-wrap">
            <span
              className={point.is_anomaly ? 'trend__bar trend__bar--flag' : 'trend__bar'}
              style={{ height: `${Math.max(2, (point.total_cents / max) * 100)}%` }}
            />
          </span>
          {point.is_anomaly && (
            <span
              className="badge badge--restricted trend__flag"
              title="Deviates from trailing baseline"
            >
              {deviationLabel(point.deviation_sigma)}
            </span>
          )}
          <span className="trend__month">{point.month.slice(2)}</span>
        </li>
      ))}
    </ul>
  );
}

function MonthlyTable({
  months,
  threshold,
}: {
  readonly months: readonly MonthlyFlagData[];
  readonly threshold?: number | undefined;
}): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>
        Monthly spend trend
        {threshold !== undefined ? ` (deviation threshold ${threshold}σ)` : ''}
      </caption>
      <thead>
        <tr>
          <th scope="col">Month</th>
          <th scope="col">Total</th>
          <th scope="col">Transactions</th>
          <th scope="col">Deviation</th>
        </tr>
      </thead>
      <tbody>
        {months.map((point) => (
          <tr key={point.month}>
            <th scope="row">{formatMonth(point.month)}</th>
            <td>{formatCents(point.total_cents)}</td>
            <td>{point.transaction_count}</td>
            <td>{point.is_anomaly ? deviationLabel(point.deviation_sigma) : 'Normal'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A signed, word-carrying label for a flagged month so meaning survives without colour. */
function deviationLabel(sigma: number | null | undefined): string {
  if (sigma === null || sigma === undefined) {
    return 'Flagged';
  }
  const magnitude = `${Math.abs(sigma).toFixed(1)}σ`;
  return sigma >= 0 ? `High +${magnitude}` : `Low −${magnitude}`;
}
