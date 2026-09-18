import { useState } from 'react';

import { ModuleState } from '../../components/ModuleState';
import { ChartWithTable } from '../dashboard/widgets/ChartWithTable';
import { formatCents, provenance } from '../dashboard/widgets/format';
import { formatCompactCents, LEAK_TYPE_LABEL, PreviewNote } from './RevenueChrome';
import type { FeeFinding, FeeRecovery, LeakType } from './revenueApi';
import { useFeeRecovery } from './useRevenue';

/**
 * Fee recovery (play 6) — money the bank already earned but never collected.
 *
 * Over years, accounts drift: a courtesy waiver is never restored, a product reprices but an account
 * stays on the retired tier, a treasury service goes live without billing, a household qualifies for
 * a relationship bundle nobody enrolled. No single line is large enough for anyone to notice, which
 * is exactly why it accumulates.
 *
 * Each finding is measured against a rule in the knowledge corpus and shows that rule, so a finding
 * is auditable rather than asserted — this play is the platform's easiest business case precisely
 * because it involves no selling, no customer acquisition and no risk, only billing correctly.
 */
export function FeeRecoveryWidget({
  customerId,
}: {
  readonly customerId: string;
}): React.JSX.Element {
  const state = useFeeRecovery(customerId);
  const loading = state.kind === 'loading';

  return (
    <ModuleState
      title="Fee recovery"
      state={{ status: loading ? 'loading' : 'ready' }}
      isEmpty={!loading && state.data.findings.length === 0}
      emptyLabel="No pricing or billing gaps found on this customer's accounts."
    >
      {!loading && <FeeRecoveryBody data={state.data} preview={state.kind === 'preview'} />}
    </ModuleState>
  );
}

function FeeRecoveryBody({
  data,
  preview,
}: {
  readonly data: FeeRecovery;
  readonly preview: boolean;
}): React.JSX.Element {
  // Local acted-on state: the endpoint that persists this does not exist yet, so acting marks the
  // row in the UI and the announcement says so plainly rather than implying a booked change.
  const [actioned, setActioned] = useState<readonly number[]>([]);
  const [note, setNote] = useState('');

  const open = data.findings.filter((finding) => !actioned.includes(finding.finding_id));

  return (
    <div className="widget fee-recovery">
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {note}
      </p>

      {preview && <PreviewNote what="fee-leakage scan" />}

      <div className="fee-recovery__headline">
        <div className="fee-recovery__headline-figure">
          <span className="fee-recovery__headline-value">
            {formatCompactCents(data.annualized_recoverable_cents)}
          </span>
          <span className="fee-recovery__headline-label">recoverable per year</span>
        </div>
        <p className="fee-recovery__headline-hint">
          {formatCents(data.monthly_recoverable_cents)} per month across {open.length} open finding
          {open.length === 1 ? '' : 's'}. Recovered in the next billing cycle — no customer
          acquisition and no credit risk.
        </p>
      </div>

      <ChartWithTable
        label={`Recoverable fee income by cause; ${byType(data.findings)
          .map(([type, cents]) => `${LEAK_TYPE_LABEL[type]} ${formatCompactCents(cents)}`)
          .join(', ')}`}
        chart={<LeakBars findings={data.findings} />}
        table={<LeakTable findings={data.findings} />}
      />

      <div className="fee-recovery__findings">
        <h3 className="widget__subhead" data-icon="🧾">
          Findings
        </h3>
        <ul className="fee-recovery__list">
          {data.findings.map((finding) => {
            const done = actioned.includes(finding.finding_id);
            return (
              <li key={finding.finding_id} className={`leak${done ? ' leak--actioned' : ''}`}>
                <div className="leak__head">
                  <span className="leak__type">{LEAK_TYPE_LABEL[finding.leak_type]}</span>
                  {done ? (
                    <span className="badge badge--active">Queued for billing</span>
                  ) : (
                    <span className="leak__annual">
                      {formatCents(finding.annualized_cents)} / yr
                    </span>
                  )}
                </div>
                <p className="leak__account">{finding.account_label}</p>
                {/* How the gap was established. Shown rather than hidden behind a tooltip: a finding
                    someone is expected to act on has to be defensible on sight. */}
                <p className="leak__evidence">
                  <span className="sr-only">Evidence: </span>
                  {finding.evidence}
                </p>
                <p className="leak__basis">
                  <span className="sr-only">Measured against: </span>
                  {finding.rule_basis}
                  <span className="leak__doc"> ({finding.doc_id})</span>
                </p>
                <div className="leak__actions">
                  <button
                    type="button"
                    className="button"
                    disabled={done}
                    onClick={() => {
                      setActioned((current) => [...current, finding.finding_id]);
                      setNote(
                        `${LEAK_TYPE_LABEL[finding.leak_type]} queued for billing review. Recorded in this session only until the revenue service is available.`,
                      );
                    }}
                  >
                    {done ? 'Queued' : 'Queue for billing'}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      </div>

      <p className="widget__provenance">{provenance(data.as_of_date, data.source_system)}</p>
    </div>
  );
}

/** Annualized recoverable amount grouped by cause, largest first. Masked rows contribute nothing. */
function byType(findings: readonly FeeFinding[]): readonly [LeakType, number][] {
  const totals = new Map<LeakType, number>();
  for (const finding of findings) {
    if (typeof finding.annualized_cents !== 'number') {
      continue;
    }
    totals.set(finding.leak_type, (totals.get(finding.leak_type) ?? 0) + finding.annualized_cents);
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1]);
}

function LeakBars({ findings }: { readonly findings: readonly FeeFinding[] }): React.JSX.Element {
  const rows = byType(findings);
  if (rows.length === 0) {
    return (
      <p className="module__empty">
        Amounts are banded for your role, so the breakdown is shown in the data table only.
      </p>
    );
  }
  const max = rows.reduce((peak, [, cents]) => Math.max(peak, cents), 1);

  return (
    <ul className="leak-bars">
      {rows.map(([type, cents]) => (
        <li key={type} className="leak-bars__row">
          <span className="leak-bars__label">{LEAK_TYPE_LABEL[type]}</span>
          <span className="leak-bars__track">
            <span
              className="leak-bars__fill"
              style={{ width: `${String(Math.max(3, (cents / max) * 100))}%` }}
            />
          </span>
          <span className="leak-bars__value">{formatCompactCents(cents)}</span>
        </li>
      ))}
    </ul>
  );
}

function LeakTable({ findings }: { readonly findings: readonly FeeFinding[] }): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Recoverable fee income by finding</caption>
      <thead>
        <tr>
          <th scope="col">Cause</th>
          <th scope="col">Account</th>
          <th scope="col">Monthly</th>
          <th scope="col">Annualized</th>
          <th scope="col">Rule</th>
        </tr>
      </thead>
      <tbody>
        {findings.map((finding) => (
          <tr key={finding.finding_id}>
            <th scope="row">{LEAK_TYPE_LABEL[finding.leak_type]}</th>
            <td>{finding.account_label}</td>
            <td>{formatCents(finding.monthly_cents)}</td>
            <td>{formatCents(finding.annualized_cents)}</td>
            <td>{finding.rule_basis}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
