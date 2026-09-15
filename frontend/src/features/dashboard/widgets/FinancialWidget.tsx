import { useState } from 'react';
import { ModuleState } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';
import { formatBps, formatCents, humanizeEnum, humanizeMasked, provenance } from './format';
import { CompositionDonut, type DonutSlice } from './MiniCharts';
import {
  pick,
  type CardDetail,
  type CreditProfileData,
  type DepositDetail,
  type FinancialProfileData,
  type HoldingData,
  type HoldingsData,
  type InvestmentDetail,
  type LoanDetail,
} from './types';
import { useSubResource } from './useSubResource';
import { deriveWidgetState } from './widgetState';

/**
 * Financial overview and holdings widget (task 13.2, requirements 5.1–5.7).
 *
 * The headline totals (deposits, loans, investments, net worth) come from the composed 360 payload;
 * the FICO headline is fetched from `/credit` and the holdings tables from `/accounts`, because the
 * aggregator composes neither. Net worth is drillable to its components (assets, liabilities and the
 * three balances that make it up). Each holding renders in a table appropriate to its product type
 * with the fields requirements 5.4–5.7 list. Every monetary value may arrive masked to a band
 * string; `formatCents` passes those through unchanged.
 */
export function FinancialWidget({
  view,
  customerId,
}: {
  readonly view: Dashboard360;
  readonly customerId: string;
}): React.JSX.Element {
  const state = deriveWidgetState(view, {
    module: 'financial',
    slots: ['financial_profile', 'household_rollup'],
  });
  const profile = pick<FinancialProfileData>(view.raw, 'financial_profile');

  const credit = useSubResource<{ credit_profile: CreditProfileData | null }>(
    `/customers/${encodeURIComponent(customerId)}/credit`,
  );
  const accounts = useSubResource<HoldingsData>(
    `/customers/${encodeURIComponent(customerId)}/accounts`,
  );

  return (
    <ModuleState title="Financial overview" state={state.model} isEmpty={state.isEmpty}>
      {profile !== undefined && (
        <div className="widget">
          <div className="metric-grid">
            <Metric
              label="Net worth"
              value={formatCents(profile.net_worth_cents)}
              icon="💎"
              tone="indigo"
              primary
            />
            <Metric
              label="Deposits"
              value={formatCents(profile.total_deposits_cents)}
              icon="🏦"
              tone="teal"
            />
            <Metric
              label="Loans"
              value={formatCents(profile.total_loans_cents)}
              icon="📉"
              tone="amber"
            />
            <Metric
              label="Investments"
              value={formatCents(profile.total_investments_cents)}
              icon="📈"
              tone="green"
            />
            <Metric label="FICO" value={ficoHeadline(credit)} icon="🎯" tone="violet" />
          </div>

          <AssetComposition profile={profile} />

          <NetWorthDrilldown profile={profile} />

          <p className="widget__provenance">
            {provenance(profile.as_of_date, profile.source_system)}
          </p>

          <HoldingsSection accounts={accounts} />
        </div>
      )}
    </ModuleState>
  );
}

function ficoHeadline(
  credit: ReturnType<typeof useSubResource<{ credit_profile: CreditProfileData | null }>>,
): string {
  if (credit.kind === 'loading') {
    return '…';
  }
  if (credit.kind === 'error') {
    return '—';
  }
  // A FICO may arrive as a real number or, for a role without full credit access, as a masked band
  // string (e.g. "VERY_LOW"). Humanize the band so it does not read as a machine token.
  return humanizeMasked(credit.data.credit_profile?.fico_score);
}

/** A finite number if the masked-or-raw value is an actual number, else null (masked/absent). */
function asAmount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * A colourful composition donut over the balances the profile already carries (deposits,
 * investments, loans). Purely a visual companion to the metric tiles and net-worth breakdown, which
 * show the same figures — so it is only rendered when those balances are real numbers (not masked
 * band strings) and at least one is positive. It never changes or re-derives data.
 */
function AssetComposition({
  profile,
}: {
  readonly profile: FinancialProfileData;
}): React.JSX.Element | null {
  const deposits = asAmount(profile.total_deposits_cents);
  const investments = asAmount(profile.total_investments_cents);
  const loans = asAmount(profile.total_loans_cents);

  if (deposits === null && investments === null && loans === null) {
    return null;
  }

  const slices: DonutSlice[] = [
    { label: 'Deposits', value: deposits ?? 0, color: '#6366f1' },
    { label: 'Investments', value: investments ?? 0, color: '#14b8a6' },
    { label: 'Loans', value: loans ?? 0, color: '#f97316' },
  ].filter((s) => s.value > 0);

  if (slices.length === 0) {
    return null;
  }

  const netWorth = asAmount(profile.net_worth_cents);

  return (
    <section className="mini-chart-card" aria-label="Balance composition">
      <h3 className="widget__subhead" data-icon="🍩">
        Balance composition
      </h3>
      <CompositionDonut
        slices={slices}
        centerLabel="Total"
        centerValue={
          netWorth !== null ? formatCents(netWorth) : formatCents(profile.net_worth_cents)
        }
      />
    </section>
  );
}

function NetWorthDrilldown({
  profile,
}: {
  readonly profile: FinancialProfileData;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <details className="drilldown" open={open}>
      <summary
        className="drilldown__summary focus-ring"
        onClick={(event) => {
          event.preventDefault();
          setOpen((prev) => !prev);
        }}
      >
        Net worth breakdown
      </summary>
      <dl className="kv">
        <Row label="Total assets" value={formatCents(profile.total_assets_cents)} />
        <Row label="Total liabilities" value={formatCents(profile.total_liabilities_cents)} />
        <Row label="Deposits" value={formatCents(profile.total_deposits_cents)} />
        <Row label="Investments" value={formatCents(profile.total_investments_cents)} />
        <Row label="Loans" value={formatCents(profile.total_loans_cents)} />
        <Row label="Household net worth" value={formatCents(profile.household_net_worth_cents)} />
        <Row label="Monthly income" value={formatCents(profile.monthly_income_cents)} />
        <Row label="Monthly expense" value={formatCents(profile.monthly_expense_cents)} />
      </dl>
    </details>
  );
}

function HoldingsSection({
  accounts,
}: {
  readonly accounts: ReturnType<typeof useSubResource<HoldingsData>>;
}): React.JSX.Element {
  if (accounts.kind === 'loading') {
    return <p className="module__loading">Loading holdings…</p>;
  }
  if (accounts.kind === 'error') {
    return (
      <p className="module__error" role="alert">
        Holdings could not be loaded.
      </p>
    );
  }
  const holdings = accounts.data.holdings ?? [];
  if (holdings.length === 0) {
    return <p className="module__empty">No holdings on file.</p>;
  }

  const deposits = holdings.filter((h) => h.account.account_type === 'DEPOSIT');
  const loans = holdings.filter((h) => h.account.account_type === 'LOAN');
  const cards = holdings.filter((h) => h.account.account_type === 'CARD');
  const investments = holdings.filter((h) => h.account.account_type === 'INVESTMENT');

  return (
    <div className="holdings">
      {deposits.length > 0 && (
        <TableScroll label="Deposits, scrollable">
          <DepositTable holdings={deposits} />
        </TableScroll>
      )}
      {loans.length > 0 && (
        <TableScroll label="Loans, scrollable">
          <LoanTable holdings={loans} />
        </TableScroll>
      )}
      {cards.length > 0 && (
        <TableScroll label="Credit cards, scrollable">
          <CardTable holdings={cards} />
        </TableScroll>
      )}
      {investments.length > 0 && (
        <TableScroll label="Investments, scrollable">
          <InvestmentTable holdings={investments} />
        </TableScroll>
      )}
    </div>
  );
}

/** Keeps a wide holdings table inside its card by scrolling horizontally rather than overflowing. */
function TableScroll({
  label,
  children,
}: {
  readonly label: string;
  readonly children: React.ReactNode;
}): React.JSX.Element {
  return (
    <div className="table-scroll" tabIndex={0} aria-label={label}>
      {children}
    </div>
  );
}

function DepositTable({
  holdings,
}: {
  readonly holdings: readonly HoldingData[];
}): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Deposits</caption>
      <thead>
        <tr>
          <th scope="col">Product</th>
          <th scope="col">Balance</th>
          <th scope="col">Rate</th>
          <th scope="col">Maturity</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {holdings.map((h) => {
          const detail = h.detail as DepositDetail | undefined;
          return (
            <tr key={h.account.account_id}>
              <th scope="row">{h.account.product_name ?? detail?.product_type ?? 'Deposit'}</th>
              <td>{formatCents(h.account.balance_cents)}</td>
              <td>{formatBps(h.account.interest_rate_bps)}</td>
              <td>{detail?.maturity_date ?? '—'}</td>
              <td>{humanizeEnum(h.account.account_status)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function LoanTable({ holdings }: { readonly holdings: readonly HoldingData[] }): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Loans</caption>
      <thead>
        <tr>
          <th scope="col">Type</th>
          <th scope="col">Balance</th>
          <th scope="col">Original</th>
          <th scope="col">EMI</th>
          <th scope="col">Rate</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {holdings.map((h) => {
          const detail = h.detail as LoanDetail | undefined;
          return (
            <tr key={h.account.account_id}>
              <th scope="row">{humanizeEnum(detail?.loan_type) || 'Loan'}</th>
              <td>{formatCents(h.account.balance_cents)}</td>
              <td>{formatCents(detail?.original_amount_cents)}</td>
              <td>{formatCents(detail?.monthly_emi_cents)}</td>
              <td>{formatBps(h.account.interest_rate_bps)}</td>
              <td>{humanizeEnum(detail?.loan_status ?? h.account.account_status)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function CardTable({ holdings }: { readonly holdings: readonly HoldingData[] }): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Credit cards</caption>
      <thead>
        <tr>
          <th scope="col">Card</th>
          <th scope="col">Balance</th>
          <th scope="col">Limit</th>
          <th scope="col">Utilization</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {holdings.map((h) => {
          const detail = h.detail as CardDetail | undefined;
          const last4 = detail?.card_last4;
          const label =
            last4 === null || last4 === undefined
              ? (h.account.product_name ?? 'Card')
              : `•••• ${last4}`;
          return (
            <tr key={h.account.account_id}>
              <th scope="row">{label}</th>
              <td>{formatCents(h.account.balance_cents)}</td>
              <td>{formatCents(detail?.credit_limit_cents)}</td>
              <td>{formatBps(detail?.utilization_bps)}</td>
              <td>{humanizeEnum(h.account.account_status)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function InvestmentTable({
  holdings,
}: {
  readonly holdings: readonly HoldingData[];
}): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Investments</caption>
      <thead>
        <tr>
          <th scope="col">Portfolio</th>
          <th scope="col">Value</th>
          <th scope="col">Risk profile</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {holdings.map((h) => {
          const detail = h.detail as InvestmentDetail | undefined;
          return (
            <tr key={h.account.account_id}>
              <th scope="row">{h.account.product_name ?? 'Investment'}</th>
              <td>{formatCents(detail?.portfolio_value_cents ?? h.account.balance_cents)}</td>
              <td>{humanizeEnum(detail?.investment_risk_profile)}</td>
              <td>{humanizeEnum(h.account.account_status)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Metric({
  label,
  value,
  primary = false,
  icon,
  tone = 'indigo',
}: {
  readonly label: string;
  readonly value: string;
  readonly primary?: boolean;
  readonly icon?: string;
  readonly tone?: string;
}): React.JSX.Element {
  return (
    <div className={`metric metric--${tone}${primary ? ' metric--primary' : ''}`}>
      {icon !== undefined && (
        <span className="metric__icon" aria-hidden="true">
          {icon}
        </span>
      )}
      <span className="metric__body">
        <span className="metric__label">{label}</span>
        <span className="metric__value">{value}</span>
      </span>
    </div>
  );
}

function Row({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}): React.JSX.Element {
  return (
    <>
      <dt className="kv__key">{label}</dt>
      <dd className="kv__val">{value}</dd>
    </>
  );
}
