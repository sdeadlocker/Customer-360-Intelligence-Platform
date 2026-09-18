import { ModuleState } from '../../components/ModuleState';
import { ChartWithTable } from '../dashboard/widgets/ChartWithTable';
import { formatCents, provenance } from '../dashboard/widgets/format';
import { CompositionDonut, type DonutSlice } from '../dashboard/widgets/MiniCharts';
import {
  formatCompactCents,
  formatConfidence,
  formatShare,
  HOLDING_TYPE_LABEL,
  PreviewNote,
} from './RevenueChrome';
import type { ExternalHolding, WalletShare } from './revenueApi';
import { useWalletShare } from './useRevenue';

/**
 * Wallet share / held-away detection (play 4) — the biggest single revenue feature.
 *
 * The bank sees only its own accounts, so a customer who looks like a modest depositor may hold
 * several hundred thousand elsewhere and carry a competitor's mortgage. This widget reads the
 * footprints those external relationships leave in the customer's own transaction history — a
 * recurring debit to a brokerage, a fixed monthly payment to a loan servicer with no matching loan on
 * our books, salary arriving as an inbound transfer rather than a direct deposit — and turns each
 * into a **dollar-sized capture target with its inference basis shown**.
 *
 * Two disciplines matter here. Every estimate states the pattern it rests on and a confidence, because
 * an inferred holding is not the same fact as a held account (the same rule the relationship graph
 * applies to inferred edges, requirement 6.6). And the donut is drawn only when both sides are real
 * numbers — a masked band string is never charted, only read.
 */
export function WalletShareWidget({
  customerId,
}: {
  readonly customerId: string;
}): React.JSX.Element {
  const state = useWalletShare(customerId);
  const loading = state.kind === 'loading';

  return (
    <ModuleState
      title="Wallet share"
      state={{ status: loading ? 'loading' : 'ready' }}
      isEmpty={!loading && state.data.holdings.length === 0}
      emptyLabel="No external holdings inferred from this customer's transaction history."
    >
      {!loading && <WalletShareBody data={state.data} preview={state.kind === 'preview'} />}
    </ModuleState>
  );
}

function WalletShareBody({
  data,
  preview,
}: {
  readonly data: WalletShare;
  readonly preview: boolean;
}): React.JSX.Element {
  return (
    <div className="widget wallet-share">
      {preview && <PreviewNote what="held-away detection" />}

      {data.is_primary_bank === false && (
        <p className="wallet-share__primacy" role="note">
          <span className="badge badge--restricted">Not primary</span> Salary does not arrive here
          as a direct deposit — primacy is held by another institution.
        </p>
      )}

      <div className="wallet-share__metrics">
        <Metric
          label="With us"
          value={formatCompactCents(data.internal_value_cents)}
          glyph="🏛"
          tone="indigo"
        />
        <Metric
          label="Held away"
          value={formatCompactCents(data.held_away_cents)}
          glyph="🏦"
          tone="amber"
        />
        <Metric
          label="Capturable"
          value={formatCompactCents(data.capturable_cents)}
          glyph="🎯"
          tone="violet"
          primary
        />
        <Metric
          label="Annual revenue if captured"
          value={formatCompactCents(data.annual_revenue_if_captured_cents)}
          glyph="📈"
          tone="green"
        />
      </div>

      <ChartWithTable
        label={`Wallet share: ${formatShare(data.wallet_share_bps)} of estimated total assets are held with us`}
        chart={<ShareDonut data={data} />}
        table={<ShareTable data={data} />}
      />

      <HoldingsList holdings={data.holdings} />

      <p className="widget__provenance">{provenance(data.as_of_date, data.source_system)}</p>
    </div>
  );
}

function Metric({
  label,
  value,
  glyph,
  tone,
  primary = false,
}: {
  readonly label: string;
  readonly value: string;
  readonly glyph: string;
  /** One of the shared metric tones already defined in the stylesheet. */
  readonly tone: 'indigo' | 'amber' | 'green' | 'teal' | 'violet';
  readonly primary?: boolean;
}): React.JSX.Element {
  return (
    <div className={`metric metric--${tone}${primary ? ' metric--primary' : ''}`}>
      <span className="metric__icon" aria-hidden="true">
        {glyph}
      </span>
      <span className="metric__label">{label}</span>
      <span className="metric__value">{value}</span>
    </div>
  );
}

/**
 * Internal versus held-away as a donut. Drawn only when both magnitudes are real numbers; a masked
 * band cannot be plotted to scale, so the chart is replaced by a note and the table carries the
 * figures instead.
 */
function ShareDonut({ data }: { readonly data: WalletShare }): React.JSX.Element {
  const internal = typeof data.internal_value_cents === 'number' ? data.internal_value_cents : null;
  const held = typeof data.held_away_cents === 'number' ? data.held_away_cents : null;

  if (internal === null || held === null) {
    return (
      <p className="module__empty">
        Composition is not shown because a value is banded for your role. The figures are in the
        data table.
      </p>
    );
  }

  const slices: readonly DonutSlice[] = [
    { label: 'With us', value: internal, color: '#6366f1' },
    { label: 'Held away', value: held, color: '#f97316' },
  ];

  return (
    <CompositionDonut
      slices={slices}
      centerLabel="with us"
      centerValue={formatShare(data.wallet_share_bps)}
    />
  );
}

function ShareTable({ data }: { readonly data: WalletShare }): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Estimated assets, with us versus held away</caption>
      <thead>
        <tr>
          <th scope="col">Where</th>
          <th scope="col">Estimated value</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <th scope="row">With us</th>
          <td>{formatCents(data.internal_value_cents)}</td>
        </tr>
        <tr>
          <th scope="row">Held away</th>
          <td>{formatCents(data.held_away_cents)}</td>
        </tr>
        <tr>
          <th scope="row">Realistically capturable</th>
          <td>{formatCents(data.capturable_cents)}</td>
        </tr>
        <tr>
          <th scope="row">Wallet share with us</th>
          <td>{formatShare(data.wallet_share_bps)}</td>
        </tr>
      </tbody>
    </table>
  );
}

/**
 * The detected external relationships, largest estimate first. Each row shows the inference basis,
 * because an inferred holding must be visibly distinguishable from a held fact.
 */
function HoldingsList({
  holdings,
}: {
  readonly holdings: readonly ExternalHolding[];
}): React.JSX.Element {
  const ordered = [...holdings].sort((a, b) => magnitude(b) - magnitude(a));

  return (
    <div className="held-away">
      <h3 className="widget__subhead" data-icon="🔍">
        Detected elsewhere
      </h3>
      <ul className="held-away__list">
        {ordered.map((holding) => (
          <li key={holding.holding_id} className="held-away__row">
            <div className="held-away__head">
              <span className="held-away__type">{HOLDING_TYPE_LABEL[holding.holding_type]}</span>
              <span className="badge badge--partial">Inferred</span>
            </div>
            <div className="held-away__figures">
              <span className="held-away__value">
                {holding.estimated_value_cents === null ||
                holding.estimated_value_cents === undefined
                  ? 'Value not estimable'
                  : `${formatCents(holding.estimated_value_cents)} estimated`}
              </span>
              <span className="held-away__flow">
                {formatCents(holding.monthly_flow_cents)} / month observed
              </span>
              <span className="held-away__confidence">{formatConfidence(holding.confidence)}</span>
            </div>
            <p className="held-away__basis">
              <span className="sr-only">Inference basis: </span>
              {holding.basis}
            </p>
            <p className="held-away__counterparty">{holding.counterparty}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Sort magnitude for a holding: its estimated value when known, otherwise its monthly flow. */
function magnitude(holding: ExternalHolding): number {
  if (typeof holding.estimated_value_cents === 'number') {
    return holding.estimated_value_cents;
  }
  return typeof holding.monthly_flow_cents === 'number' ? holding.monthly_flow_cents : 0;
}
