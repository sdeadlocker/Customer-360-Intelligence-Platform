import { ModuleState } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';
import { ChartWithTable } from './ChartWithTable';
import { formatCents, humanizeEnum, humanizeMasked, provenance, type Maskable } from './format';
import { pick, type RiskAlertData, type RiskData } from './types';
import { deriveWidgetState } from './widgetState';

/**
 * Risk analysis widget (task 13.4, requirements 8.1–8.7).
 *
 * A risk gauge showing the band, the ranked risk drivers (the composite sub-scores), the alerts in
 * severity order, and — when the customer carries an AML or PEP flag — a persistent, non-dismissible
 * compliance banner (requirement 8.4). A role without the risk entitlement never sees the raw score:
 * the server masks `risk_score` to a coarse band string, so this widget shows only the band it was
 * given (requirement 8.7) and the gauge positions from the band, not a hidden number.
 *
 * The band is never encoded by colour alone (requirement 16.1): the gauge shows the band word, and
 * every alert row states its severity in text.
 */

const BAND_ORDER = ['LOW', 'MODERATE', 'ELEVATED', 'HIGH'] as const;
type Band = (typeof BAND_ORDER)[number];

export function RiskWidget({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const state = deriveWidgetState(view, { module: 'risk', slots: ['risk'] });
  const risk = pick<RiskData>(view.raw, 'risk');

  return (
    <ModuleState title="Risk analysis" state={state.model} isEmpty={state.isEmpty}>
      {risk !== undefined && (
        <div className="widget">
          {risk.requires_compliance_indicator && (
            <div className="banner banner--compliance" role="alert">
              <strong>Compliance review required.</strong> This customer carries an AML or PEP
              indicator. This notice cannot be dismissed.
            </div>
          )}

          <ChartWithTable
            label={`Risk gauge; current band ${humanizeEnum(risk.band)}`}
            chart={<RiskGauge band={risk.band} />}
            table={<GaugeTable band={risk.band} />}
          />

          <RiskDrivers risk={risk} />

          <Alerts alerts={risk.alerts ?? []} />

          <dl className="kv">
            <Row label="Credit exposure" value={formatCents(risk.credit_exposure_cents)} />
            <Row label="Days past due" value={dpd(risk)} />
            <Row
              label="Delinquency"
              value={humanizeEnum(coerce(risk.profile.delinquency_status))}
            />
          </dl>

          <p className="widget__provenance">
            {provenance(risk.profile.as_of_date, risk.profile.source_system)}
          </p>
        </div>
      )}
    </ModuleState>
  );
}

function RiskGauge({ band }: { readonly band: string }): React.JSX.Element {
  const normalized = band.toUpperCase() as Band;
  const index = BAND_ORDER.indexOf(normalized);
  const position = index >= 0 ? (index + 0.5) / BAND_ORDER.length : 0.5;
  return (
    <div className="gauge" aria-label={`Risk band: ${humanizeEnum(band)}`}>
      <div className="gauge__track" aria-hidden="true">
        {BAND_ORDER.map((segment, i) => (
          <span
            key={segment}
            className={`gauge__seg gauge__seg--${segment.toLowerCase()}${i === index ? ' gauge__seg--active' : ''}`}
          />
        ))}
        <span className="gauge__needle" style={{ left: `${position * 100}%` }} />
      </div>
      <p className="gauge__label">
        Risk band: <strong>{humanizeEnum(band)}</strong>
      </p>
    </div>
  );
}

/**
 * The accessible table equivalent of the risk gauge (task 15.2, requirement 16.2). The gauge is a
 * visual only; this table names every band on the scale, ordered from low to high, and marks which
 * one the customer sits in — the same information the needle position conveys, readable by a screen
 * reader.
 */
function GaugeTable({ band }: { readonly band: string }): React.JSX.Element {
  const normalized = band.toUpperCase();
  return (
    <table className="data-table">
      <caption>Risk band scale</caption>
      <thead>
        <tr>
          <th scope="col">Band</th>
          <th scope="col">Current</th>
        </tr>
      </thead>
      <tbody>
        {BAND_ORDER.map((segment) => (
          <tr key={segment}>
            <th scope="row">{humanizeEnum(segment)}</th>
            <td>{segment === normalized ? 'Current band' : '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Ranked drivers: the composite sub-scores, highest first. Each is masked to a band string for a
 * role without the risk entitlement, so a value that is a string is shown verbatim and not ranked
 * numerically.
 */
function RiskDrivers({ risk }: { readonly risk: RiskData }): React.JSX.Element {
  const drivers = [
    { label: 'Overall risk', value: risk.profile.risk_score },
    { label: 'Fraud', value: risk.profile.fraud_score },
    { label: 'Primary ID (PID)', value: risk.profile.pid_score },
    { label: 'Secondary ID (SID)', value: risk.profile.sid_score },
  ].filter((driver) => driver.value !== null && driver.value !== undefined);

  if (drivers.length === 0) {
    return <p className="module__empty">No risk drivers available for your role.</p>;
  }

  const numeric = drivers.filter((d) => typeof d.value === 'number') as {
    label: string;
    value: number;
  }[];
  numeric.sort((a, b) => b.value - a.value);
  const ordered = numeric.length === drivers.length ? numeric : drivers;

  return (
    <div className="drivers">
      <h3 className="widget__subhead" data-icon="🧭">
        Risk drivers
      </h3>
      <ul className="drivers__list">
        {ordered.map((driver) => (
          <li key={driver.label} className="drivers__row">
            <span className="drivers__label">{driver.label}</span>
            <span className="drivers__value">{humanizeMasked(driver.value)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Alerts({ alerts }: { readonly alerts: readonly RiskAlertData[] }): React.JSX.Element {
  if (alerts.length === 0) {
    return <p className="module__empty">No active risk alerts.</p>;
  }
  const ordered = [...alerts].sort((a, b) => b.severity - a.severity);
  return (
    <div className="alerts">
      <h3 className="widget__subhead" data-icon="🚨">
        Alerts
      </h3>
      <ul className="alerts__list">
        {ordered.map((alert, i) => (
          <li key={`${alert.category}-${i}`} className={`alert alert--sev-${alert.severity}`}>
            <span className="alert__sev">{severityLabel(alert.severity)}</span>
            <span className="alert__cat">{humanizeEnum(alert.category)}</span>
            <span className="alert__detail">{alert.detail}</span>
            {!alert.dismissible && <span className="badge">Persistent</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function severityLabel(severity: number): string {
  if (severity >= 3) {
    return 'Critical';
  }
  if (severity === 2) {
    return 'Warning';
  }
  return 'Info';
}

function dpd(risk: RiskData): string {
  const value = risk.profile.current_days_past_due;
  if (value === null || value === undefined) {
    return '—';
  }
  return typeof value === 'number' ? String(value) : value;
}

function coerce(value: Maskable): string | null {
  if (value === null || value === undefined || value === '') {
    return null;
  }
  return typeof value === 'number' ? String(value) : value;
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
