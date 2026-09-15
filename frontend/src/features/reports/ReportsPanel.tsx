import { useCallback, useId, useState } from 'react';

import {
  type Cadence,
  type ReportType,
  type RunView,
  defineReport,
  disableSchedule,
  downloadArtifactUrl,
  listRuns,
  runReport,
  scheduleReport,
} from './reportsApi';

/**
 * The report exports panel (task 18.6).
 *
 * For the customer in view, an RM can: define a branded 360 PDF pack or a prepare-for-meeting
 * briefing (with a live branding preview), trigger a run, download the produced artifact, view the
 * run history, and attach or disable a schedule. Every control is a labelled, keyboard-navigable
 * element; a single polite live region announces the outcome of each action once, rather than a
 * per-row readout.
 *
 * The panel holds no customer data itself — it drives the report endpoints, which generate from the
 * masked, entitlement-scoped service reads (task 18.2). Its state is the local definition/run
 * bookkeeping only.
 */

const REPORT_TYPE_LABEL: Record<ReportType, string> = {
  PDF_PACK: 'Branded 360 PDF pack',
  MEETING_BRIEFING: 'Prepare-for-meeting briefing',
};

/**
 * Presentational metadata for each report type — an emoji chip, a one-line description, an accent
 * colour used across the card, cover band and preview art, and an inline illustration. The art is
 * pure SVG (no external assets: works offline, scales crisply, and re-colours per theme).
 */
interface ReportTypeMeta {
  readonly icon: string;
  readonly blurb: string;
  readonly accent: string;
  readonly art: (accent: string) => React.JSX.Element;
}

/** A stacked-document illustration for the PDF pack. */
function PackArt(accent: string): React.JSX.Element {
  return (
    <svg viewBox="0 0 64 64" className="reports-type-card__art-svg" aria-hidden="true">
      <rect x="16" y="12" width="30" height="40" rx="4" fill={accent} opacity="0.18" />
      <rect
        x="12"
        y="8"
        width="30"
        height="40"
        rx="4"
        fill="var(--color-surface)"
        stroke={accent}
        strokeWidth="2"
      />
      <rect x="18" y="16" width="18" height="3" rx="1.5" fill={accent} />
      <rect x="18" y="23" width="14" height="2.5" rx="1.25" fill={accent} opacity="0.5" />
      <rect x="18" y="34" width="5" height="8" rx="1" fill={accent} opacity="0.7" />
      <rect x="25" y="30" width="5" height="12" rx="1" fill={accent} />
      <rect x="32" y="26" width="5" height="16" rx="1" fill={accent} opacity="0.85" />
    </svg>
  );
}

/** A calendar + talking-points illustration for the meeting briefing. */
function BriefingArt(accent: string): React.JSX.Element {
  return (
    <svg viewBox="0 0 64 64" className="reports-type-card__art-svg" aria-hidden="true">
      <rect
        x="12"
        y="14"
        width="34"
        height="30"
        rx="4"
        fill="var(--color-surface)"
        stroke={accent}
        strokeWidth="2"
      />
      <rect x="12" y="14" width="34" height="8" rx="4" fill={accent} />
      <rect x="19" y="10" width="3" height="8" rx="1.5" fill={accent} />
      <rect x="36" y="10" width="3" height="8" rx="1.5" fill={accent} />
      <circle cx="21" cy="30" r="2.5" fill={accent} />
      <rect x="26" y="28.5" width="14" height="3" rx="1.5" fill={accent} opacity="0.5" />
      <circle cx="21" cy="38" r="2.5" fill={accent} opacity="0.7" />
      <rect x="26" y="36.5" width="10" height="3" rx="1.5" fill={accent} opacity="0.35" />
    </svg>
  );
}

const REPORT_TYPE_META: Record<ReportType, ReportTypeMeta> = {
  PDF_PACK: {
    icon: '📊',
    blurb: 'A full, branded 360 dossier — profile, finances, risk and relationships.',
    accent: 'var(--color-accent-strong)',
    art: PackArt,
  },
  MEETING_BRIEFING: {
    icon: '🗓️',
    blurb: 'A concise pre-meeting brief with talking points and open signals.',
    accent: 'var(--color-pass)',
    art: BriefingArt,
  },
};

const REPORT_TYPES: readonly ReportType[] = ['PDF_PACK', 'MEETING_BRIEFING'];

const CADENCES: readonly Cadence[] = ['DAILY', 'WEEKLY', 'MONTHLY'];

/** A run's status mapped to a display label and a glyph — colour is reinforced by both, never alone. */
const STATUS_META: Record<
  string,
  { readonly label: string; readonly glyph: string; readonly tone: string }
> = {
  SUCCEEDED: { label: 'Succeeded', glyph: '✓', tone: 'pass' },
  RUNNING: { label: 'Running', glyph: '◷', tone: 'info' },
  PENDING: { label: 'Pending', glyph: '◷', tone: 'info' },
  FAILED: { label: 'Failed', glyph: '✕', tone: 'fail' },
};

function statusMeta(status: string): { label: string; glyph: string; tone: string } {
  return STATUS_META[status] ?? { label: status, glyph: '•', tone: 'muted' };
}

/** Format an ISO timestamp for the run history, or a dash when absent. */
function formatWhen(value: string | null): string {
  if (value === null || value === '') {
    return '—';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function ReportsPanel({ customerId }: { readonly customerId: string }): React.JSX.Element {
  const titleId = useId();
  const typeId = useId();
  const brandId = useId();
  const taglineId = useId();
  const cadenceId = useId();

  const [reportType, setReportType] = useState<ReportType>('PDF_PACK');
  const [title, setTitle] = useState('360 report');
  const [brandName, setBrandName] = useState('Customer 360 Bank');
  const [tagline, setTagline] = useState('Relationship intelligence, on demand');
  const [cadence, setCadence] = useState<Cadence>('WEEKLY');

  const [definitionId, setDefinitionId] = useState<number | null>(null);
  const [scheduleId, setScheduleId] = useState<number | null>(null);
  const [runs, setRuns] = useState<readonly RunView[]>([]);
  const [busy, setBusy] = useState(false);
  const [announcement, setAnnouncement] = useState('');

  const refreshRuns = useCallback(async (id: number) => {
    const latest = await listRuns(id);
    setRuns(latest);
  }, []);

  const onDefineAndRun = useCallback(() => {
    setBusy(true);
    setAnnouncement('Generating report…');
    void (async () => {
      try {
        const id = await defineReport({
          report_type: reportType,
          title,
          scope: { kind: 'CUSTOMER', customer_id: customerId },
          branding: { brand_name: brandName, tagline },
        });
        setDefinitionId(id);
        const run = await runReport(id);
        await refreshRuns(id);
        setAnnouncement(
          run.status === 'SUCCEEDED'
            ? `Report generated: ${String(run.customers_rendered)} customer${
                run.customers_rendered === 1 ? '' : 's'
              }.`
            : `Report run ${run.status.toLowerCase()}.`,
        );
      } catch {
        setAnnouncement('The report could not be generated.');
      } finally {
        setBusy(false);
      }
    })();
  }, [reportType, title, customerId, brandName, tagline, refreshRuns]);

  const onSchedule = useCallback(() => {
    if (definitionId === null) {
      setAnnouncement('Define and run a report before scheduling it.');
      return;
    }
    void (async () => {
      try {
        const id = await scheduleReport(definitionId, cadence);
        setScheduleId(id);
        setAnnouncement(`Scheduled ${cadence.toLowerCase()}.`);
      } catch {
        setAnnouncement('The schedule could not be created.');
      }
    })();
  }, [definitionId, cadence]);

  const onUnschedule = useCallback(() => {
    if (scheduleId === null) {
      return;
    }
    void (async () => {
      try {
        await disableSchedule(scheduleId);
        setScheduleId(null);
        setAnnouncement('Schedule disabled.');
      } catch {
        setAnnouncement('The schedule could not be disabled.');
      }
    })();
  }, [scheduleId]);

  const onDownload = useCallback((runId: number) => {
    void (async () => {
      try {
        const url = await downloadArtifactUrl(runId);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `report-run-${String(runId)}.pdf`;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        // Revoke after a tick so the click has started the download.
        setTimeout(() => {
          URL.revokeObjectURL(url);
        }, 0);
      } catch {
        setAnnouncement('The artifact could not be downloaded.');
      }
    })();
  }, []);

  return (
    <section className="module reports-panel" aria-labelledby="reports-panel-title">
      <div className="module__head">
        <div className="reports-panel__heading">
          <span className="reports-panel__heading-icon" aria-hidden="true">
            📄
          </span>
          <div>
            <h2 id="reports-panel-title" className="module__title">
              Reports
            </h2>
            <p className="reports-panel__subtitle">
              Compose a branded, entitlement-scoped export for this customer and download or
              schedule it.
            </p>
          </div>
        </div>
      </div>

      <div className="module__body">
        <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {announcement}
        </p>

        <form
          className="reports-panel__form"
          onSubmit={(event) => {
            event.preventDefault();
            onDefineAndRun();
          }}
        >
          {/* Report type as a pair of selectable cards, not a bare dropdown. */}
          <fieldset className="reports-panel__types">
            <legend id={typeId} className="reports-panel__legend">
              Report type
            </legend>
            <div className="reports-panel__type-grid" role="radiogroup" aria-labelledby={typeId}>
              {REPORT_TYPES.map((type) => {
                const selected = reportType === type;
                const meta = REPORT_TYPE_META[type];
                return (
                  <button
                    key={type}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    className={`reports-type-card focus-ring${selected ? ' reports-type-card--selected' : ''}`}
                    style={{ ['--card-accent' as string]: meta.accent }}
                    onClick={() => {
                      setReportType(type);
                    }}
                  >
                    <span className="reports-type-card__art" aria-hidden="true">
                      {meta.art(meta.accent)}
                    </span>
                    <span className="reports-type-card__text">
                      <span className="reports-type-card__label">
                        <span className="reports-type-card__icon" aria-hidden="true">
                          {meta.icon}
                        </span>
                        {REPORT_TYPE_LABEL[type]}
                      </span>
                      <span className="reports-type-card__blurb">{meta.blurb}</span>
                    </span>
                    {selected && (
                      <span className="reports-type-card__check" aria-hidden="true">
                        ✓
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </fieldset>

          <div className="reports-panel__compose">
            <div className="reports-panel__inputs">
              <div className="reports-panel__field">
                {/* The glyph is drawn by CSS (data-icon) so the label's accessible name stays
                    exactly "Title" — a decorative emoji in the label text would pollute it. */}
                <label htmlFor={titleId} data-icon="📝">
                  Title
                </label>
                <input
                  id={titleId}
                  type="text"
                  value={title}
                  maxLength={200}
                  onChange={(event) => {
                    setTitle(event.target.value);
                  }}
                />
              </div>

              <div className="reports-panel__field">
                <label htmlFor={brandId} data-icon="🏦">
                  Brand name
                </label>
                <input
                  id={brandId}
                  type="text"
                  value={brandName}
                  onChange={(event) => {
                    setBrandName(event.target.value);
                  }}
                />
              </div>

              <div className="reports-panel__field">
                <label htmlFor={taglineId} data-icon="✨">
                  Tagline
                </label>
                <input
                  id={taglineId}
                  type="text"
                  value={tagline}
                  onChange={(event) => {
                    setTagline(event.target.value);
                  }}
                />
              </div>
            </div>

            {/* Branding preview — a value-free cover mock so the RM sees the header before running. */}
            <div
              className="reports-panel__preview"
              aria-label="Branding preview"
              style={{ ['--cover-accent' as string]: REPORT_TYPE_META[reportType].accent }}
            >
              <div className="reports-panel__preview-band" aria-hidden="true">
                <span className="reports-panel__preview-badge" aria-hidden="true">
                  {REPORT_TYPE_META[reportType].icon}
                </span>
              </div>
              <div className="reports-panel__preview-body">
                <span className="reports-panel__preview-kind">{REPORT_TYPE_LABEL[reportType]}</span>
                <span className="reports-panel__preview-brand">{brandName || 'Brand name'}</span>
                {tagline !== '' && (
                  <span className="reports-panel__preview-tagline">{tagline}</span>
                )}
                {/* A little cover illustration so the preview reads as a report, not a text block. */}
                <span className="reports-panel__preview-art" aria-hidden="true">
                  <svg viewBox="0 0 220 90" className="reports-panel__preview-svg">
                    <defs>
                      <linearGradient id="cover-bars" x1="0" y1="1" x2="0" y2="0">
                        <stop offset="0%" stopColor="var(--cover-accent)" stopOpacity="0.35" />
                        <stop offset="100%" stopColor="var(--cover-accent)" stopOpacity="0.9" />
                      </linearGradient>
                    </defs>
                    <rect x="10" y="52" width="20" height="28" rx="3" fill="url(#cover-bars)" />
                    <rect x="38" y="40" width="20" height="40" rx="3" fill="url(#cover-bars)" />
                    <rect x="66" y="30" width="20" height="50" rx="3" fill="url(#cover-bars)" />
                    <rect x="94" y="46" width="20" height="34" rx="3" fill="url(#cover-bars)" />
                    <polyline
                      points="14,34 46,24 78,14 110,22 150,10 190,6"
                      fill="none"
                      stroke="var(--cover-accent)"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                    <circle
                      cx="150"
                      cy="60"
                      r="16"
                      fill="none"
                      stroke="var(--cover-accent)"
                      strokeWidth="5"
                      opacity="0.3"
                    />
                    <circle
                      cx="150"
                      cy="60"
                      r="16"
                      fill="none"
                      stroke="var(--cover-accent)"
                      strokeWidth="5"
                      strokeDasharray="70 40"
                      strokeLinecap="round"
                      transform="rotate(-90 150 60)"
                    />
                  </svg>
                </span>
                <span className="reports-panel__preview-title">{title || 'Report title'}</span>
                <span className="reports-panel__preview-meta">
                  <span className="reports-panel__preview-avatar" aria-hidden="true">
                    👤
                  </span>
                  <span className="mono">Customer {customerId}</span>
                </span>
              </div>
            </div>
          </div>

          <button type="submit" className="button button--primary" disabled={busy}>
            {busy ? 'Generating…' : 'Generate report'}
          </button>
        </form>

        <div className="reports-panel__section reports-panel__schedule">
          <span className="reports-panel__schedule-icon" aria-hidden="true">
            🔁
          </span>
          <div className="reports-panel__field">
            <label htmlFor={cadenceId}>Schedule cadence</label>
            <select
              id={cadenceId}
              value={cadence}
              onChange={(event) => {
                setCadence(event.target.value as Cadence);
              }}
            >
              {CADENCES.map((c) => (
                <option key={c} value={c}>
                  {c[0] + c.slice(1).toLowerCase()}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="button"
            onClick={onSchedule}
            disabled={definitionId === null}
          >
            Schedule
          </button>
          {scheduleId !== null && (
            <span className="reports-panel__schedule-active">
              <span className="badge badge--pass">Scheduled {cadence.toLowerCase()}</span>
              <button type="button" className="button button--subtle" onClick={onUnschedule}>
                Disable schedule
              </button>
            </span>
          )}
        </div>

        <div className="reports-panel__runs-head">
          <h3 className="reports-panel__runs-title">Run history</h3>
          {runs.length > 0 && (
            <span className="reports-panel__runs-count">
              {runs.length} run{runs.length === 1 ? '' : 's'}
            </span>
          )}
        </div>

        {runs.length === 0 ? (
          <div className="reports-panel__empty">
            <span className="reports-panel__empty-icon" aria-hidden="true">
              🗂️
            </span>
            <p className="reports-panel__empty-text">
              No runs yet. Generate a report to see its history here.
            </p>
          </div>
        ) : (
          <ul className="reports-panel__runs" aria-label="Report run history, newest first">
            {runs.map((run) => {
              const meta = statusMeta(run.status);
              const typeLabel = REPORT_TYPE_LABEL[run.report_type as ReportType] ?? run.report_type;
              return (
                <li key={run.run_id} className="report-run">
                  <span
                    className={`report-run__status report-run__status--${meta.tone}`}
                    title={meta.label}
                  >
                    <span className="report-run__status-glyph" aria-hidden="true">
                      {meta.glyph}
                    </span>
                    {meta.label}
                  </span>

                  <span className="report-run__body">
                    <span className="report-run__title">{typeLabel}</span>
                    <span className="report-run__meta">
                      <span className="mono">Run #{run.run_id}</span>
                      <span aria-hidden="true">·</span>
                      <span>
                        {run.customers_rendered} customer{run.customers_rendered === 1 ? '' : 's'}
                      </span>
                      <span aria-hidden="true">·</span>
                      <span>{formatWhen(run.finished_at ?? run.started_at)}</span>
                      {run.degraded && (
                        <span
                          className="chip chip--muted"
                          title="Deterministic fallback narratives"
                        >
                          degraded
                        </span>
                      )}
                    </span>
                  </span>

                  {run.status === 'SUCCEEDED' ? (
                    <button
                      type="button"
                      className="button button--subtle report-run__download focus-ring"
                      onClick={() => {
                        onDownload(run.run_id);
                      }}
                    >
                      <span aria-hidden="true">⬇️</span> Download PDF
                    </button>
                  ) : (
                    <span className="report-run__no-artifact" aria-hidden="true">
                      —
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
