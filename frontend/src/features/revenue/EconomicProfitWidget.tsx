import { ModuleState } from '../../components/ModuleState';
import { ChartWithTable } from '../dashboard/widgets/ChartWithTable';
import { formatCents, humanizeEnum, provenance } from '../dashboard/widgets/format';
import { formatCompactCents, PreviewNote } from './RevenueChrome';
import type { EconomicProfit, ProfitComponent } from './revenueApi';
import { useEconomicProfit } from './useRevenue';

/**
 * Risk-adjusted economic profit (play 8, per customer).
 *
 * The dashboard's `customer_value` tier ranks customers by size. Size is not profit: a large balance
 * sitting in a high-rate savings account can cost the bank money, while a smaller relationship with a
 * mortgage and an active card earns well. An RM's time is fixed, so ranking by the wrong number wastes
 * the bank's most expensive resource.
 *
 * This card states what the customer is actually worth:
 *
 *     economic profit = deposit spread + loan spread + fees + interchange
 *                     − cost to serve − expected credit loss − cost of allocated capital
 *
 * All inputs are already in the platform (balances and rates in basis points, transaction channels for
 * cost-to-serve, FICO/DPD/exposure for expected loss), all integer cents, all deterministic — the
 * arithmetic happens server-side and this widget only narrates it. The headline insight is the **tier
 * mismatch**: where the balance tier and the profit tier disagree, the bank is pointing effort at the
 * wrong customer.
 */
export function EconomicProfitWidget({
  customerId,
}: {
  readonly customerId: string;
}): React.JSX.Element {
  const state = useEconomicProfit(customerId);
  const loading = state.kind === 'loading';

  return (
    <ModuleState
      title="Economic profit"
      state={{ status: loading ? 'loading' : 'ready' }}
      isEmpty={!loading && state.data.components.length === 0}
      emptyLabel="Economic profit has not been computed for this customer."
    >
      {!loading && <ProfitBody data={state.data} preview={state.kind === 'preview'} />}
    </ModuleState>
  );
}

function ProfitBody({
  data,
  preview,
}: {
  readonly data: EconomicProfit;
  readonly preview: boolean;
}): React.JSX.Element {
  return (
    <div className="widget economic-profit">
      {preview && <PreviewNote what="economic-profit" />}

      <div className="economic-profit__headline">
        <div className="economic-profit__figure">
          <span className="economic-profit__value">
            {formatCompactCents(data.economic_profit_cents)}
          </span>
          <span className="economic-profit__unit">economic profit / year</span>
        </div>
        <div className="economic-profit__bands">
          <span className="economic-profit__band">{humanizeEnum(data.profit_band)}</span>
          <span className="economic-profit__median">
            Segment median {formatCompactCents(data.segment_median_cents)}
          </span>
        </div>
      </div>

      <TierComparison data={data} />

      <ChartWithTable
        label={`Economic profit build-up; ${data.components
          .map(
            (component) =>
              `${component.label} ${component.direction === 'DEBIT' ? 'less' : 'plus'} ${formatCompactCents(component.amount_cents)}`,
          )
          .join(', ')}`}
        chart={<ProfitWaterfall components={data.components} />}
        table={<ProfitTable data={data} />}
      />

      <p className="widget__provenance">{provenance(data.as_of_date, data.source_system)}</p>
    </div>
  );
}

/**
 * Balance tier versus profit tier. When they disagree this is the single most actionable line on the
 * dashboard, so it is stated in words and not left to a colour or an icon.
 */
function TierComparison({ data }: { readonly data: EconomicProfit }): React.JSX.Element {
  return (
    <div className={`tier-compare${data.tier_mismatch ? ' tier-compare--mismatch' : ''}`}>
      <div className="tier-compare__pair">
        <div className="tier-compare__item">
          <span className="tier-compare__label">Balance tier</span>
          <span className="tier-compare__tier">{humanizeEnum(data.balance_tier)}</span>
          <span className="tier-compare__note">how the book ranks them today</span>
        </div>
        <span className="tier-compare__arrow" aria-hidden="true">
          →
        </span>
        <div className="tier-compare__item">
          <span className="tier-compare__label">Profit tier</span>
          <span className="tier-compare__tier">{humanizeEnum(data.profit_tier)}</span>
          <span className="tier-compare__note">what they actually earn</span>
        </div>
      </div>
      {data.tier_mismatch && (
        <p className="tier-compare__verdict" role="note">
          <span className="badge badge--restricted">Mismatch</span> This customer is ranked{' '}
          {humanizeEnum(data.balance_tier).toLowerCase()} by balance but earns like{' '}
          {humanizeEnum(data.profit_tier).toLowerCase()}. Servicing effort is likely mispriced
          against the return.
        </p>
      )}
    </div>
  );
}

/**
 * The profit build-up as a two-sided bar set: credits above the axis, debits below. Decorative
 * reinforcement — the same components, signed, are in the table equivalent.
 */
function ProfitWaterfall({
  components,
}: {
  readonly components: readonly ProfitComponent[];
}): React.JSX.Element {
  const numeric = components.filter(
    (component): component is ProfitComponent & { amount_cents: number } =>
      typeof component.amount_cents === 'number',
  );

  if (numeric.length === 0) {
    return (
      <p className="module__empty">
        Component amounts are banded for your role; the build-up is in the data table.
      </p>
    );
  }

  const max = numeric.reduce((peak, component) => Math.max(peak, component.amount_cents), 1);

  return (
    <ul className="waterfall">
      {numeric.map((component) => {
        const height = Math.max(6, (component.amount_cents / max) * 100);
        const debit = component.direction === 'DEBIT';
        return (
          <li key={component.key} className="waterfall__col">
            <span className="waterfall__bar-wrap">
              <span
                className={`waterfall__bar waterfall__bar--${debit ? 'debit' : 'credit'}`}
                style={{ height: `${String(height)}%` }}
              />
            </span>
            <span className="waterfall__amount">
              {debit ? '−' : '+'}
              {formatCompactCents(component.amount_cents)}
            </span>
            <span className="waterfall__label">{component.label}</span>
          </li>
        );
      })}
    </ul>
  );
}

function ProfitTable({ data }: { readonly data: EconomicProfit }): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Economic profit build-up</caption>
      <thead>
        <tr>
          <th scope="col">Component</th>
          <th scope="col">Effect</th>
          <th scope="col">Amount per year</th>
        </tr>
      </thead>
      <tbody>
        {data.components.map((component) => (
          <tr key={component.key}>
            <th scope="row">{component.label}</th>
            <td>{component.direction === 'DEBIT' ? 'Subtracts' : 'Adds'}</td>
            <td>{formatCents(component.amount_cents)}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row">Economic profit</th>
          <td>Net</td>
          <td>{formatCents(data.economic_profit_cents)}</td>
        </tr>
      </tfoot>
    </table>
  );
}
