import { ChartWithTable } from '../dashboard/widgets/ChartWithTable';
import { formatCents } from '../dashboard/widgets/format';
import {
  formatCompactCents,
  formatShare,
  PLAY_GLYPH,
  PLAY_LABEL,
  PreviewBadge,
} from './RevenueChrome';
import type { OpportunityPipeline, PipelinePlayLine } from './revenueApi';
import { usePipeline } from './useRevenue';

/**
 * The revenue command centre (play 8 — Opportunity P&L), the landing page's hero panel.
 *
 * This is the executive view of the platform: instead of opening with "here is a search box", the
 * first thing on screen is the priced pipeline the four revenue plays have found across the caller's
 * entitled book — total identified, how much of it is realistically capturable, how much has actually
 * been realized, and the realization rate that proves the recommendations work. Everything is
 * entitlement-scoped by the server, so an RM sees their book and a regional head sees theirs.
 *
 * The illustration is decorative and `aria-hidden`; every figure it sits beside is real text, and the
 * per-play bar chart carries a full data-table equivalent (requirement 16.2). Money is integer cents
 * end to end and only becomes a formatted string at the leaf (design §1.1).
 */
export function RevenueHero({
  onOpenUrgent,
}: {
  /** Jump to the money-in-motion feed — the urgent, time-boxed slice of the pipeline. */
  readonly onOpenUrgent: () => void;
}): React.JSX.Element {
  const pipeline = usePipeline();

  return (
    <section className="revenue-hero" aria-labelledby="revenue-hero-title">
      {/* Decorative gradient wash and orbs behind the panel. */}
      <div className="revenue-hero__decor" aria-hidden="true">
        <span className="revenue-hero__orb revenue-hero__orb--1" />
        <span className="revenue-hero__orb revenue-hero__orb--2" />
        <span className="revenue-hero__grid-lines" />
      </div>

      <div className="revenue-hero__inner">
        <div className="revenue-hero__main">
          <div className="revenue-hero__heading">
            <p className="revenue-hero__eyebrow">
              <span aria-hidden="true">◆</span> Revenue intelligence
            </p>
            <h2 id="revenue-hero-title" className="revenue-hero__title">
              Opportunity pipeline
            </h2>
            <p className="revenue-hero__lede">
              Every opportunity the platform has priced across your book, with what has actually
              been realized.
            </p>
          </div>

          {/* One atomic live region for the whole panel. */}
          <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
            {pipeline.announcement}
          </p>

          {pipeline.state.kind === 'loading' ? (
            <p className="revenue-hero__loading" role="status">
              Loading the opportunity pipeline…
            </p>
          ) : (
            <PipelineBody
              data={pipeline.state.data}
              preview={pipeline.state.kind === 'preview'}
              onOpenUrgent={onOpenUrgent}
              onReload={pipeline.reload}
            />
          )}
        </div>

        <div className="revenue-hero__art" aria-hidden="true">
          <GrowthIllustration />
        </div>
      </div>
    </section>
  );
}

function PipelineBody({
  data,
  preview,
  onOpenUrgent,
  onReload,
}: {
  readonly data: OpportunityPipeline;
  readonly preview: boolean;
  readonly onOpenUrgent: () => void;
  readonly onReload: () => void;
}): React.JSX.Element {
  return (
    <>
      <dl className="revenue-hero__kpis">
        <Kpi
          label="Identified"
          value={formatCompactCents(data.identified_cents)}
          hint={`across ${String(data.book_size)} customers`}
          glyph="🔎"
        />
        <Kpi
          label="Capturable"
          value={formatCompactCents(data.capturable_cents)}
          hint="realistic, after suppression"
          glyph="🎯"
          primary
        />
        <Kpi
          label="Realized"
          value={formatCompactCents(data.realized_cents)}
          hint="booked from acted opportunities"
          glyph="✅"
        />
        <Kpi
          label="Realization rate"
          value={formatShare(data.realization_bps)}
          hint="realized ÷ capturable"
          glyph="📈"
        />
      </dl>

      {data.urgent_count > 0 && (
        <div className="revenue-hero__urgent">
          <span className="revenue-hero__urgent-pulse" aria-hidden="true" />
          <p className="revenue-hero__urgent-text">
            <strong>
              {data.urgent_count} opportunit{data.urgent_count === 1 ? 'y' : 'ies'} need action
              today
            </strong>{' '}
            — money-in-motion windows close within 24 hours.
          </p>
          <button
            type="button"
            className="button button--primary revenue-hero__urgent-cta focus-ring"
            onClick={onOpenUrgent}
          >
            Review now
          </button>
        </div>
      )}

      <div className="revenue-hero__breakdown">
        <div className="revenue-hero__breakdown-head">
          <h3 className="revenue-hero__subhead">By revenue play</h3>
          <div className="revenue-hero__breakdown-actions">
            {preview && <PreviewBadge />}
            <button
              type="button"
              className="button button--subtle focus-ring"
              onClick={onReload}
              aria-label="Refresh the opportunity pipeline"
            >
              Refresh
            </button>
          </div>
        </div>

        <ChartWithTable
          label={`Capturable opportunity by revenue play; ${data.plays
            .map((p) => `${PLAY_LABEL[p.play]} ${formatCompactCents(p.capturable_cents)}`)
            .join(', ')}`}
          chart={<PlayBars plays={data.plays} />}
          table={<PlayTable plays={data.plays} />}
        />
      </div>

      <p className="revenue-hero__provenance">
        As of {data.as_of} · {data.source_system}
        {preview && ' · illustrative preview, not grounded data'}
      </p>
    </>
  );
}

function Kpi({
  label,
  value,
  hint,
  glyph,
  primary = false,
}: {
  readonly label: string;
  readonly value: string;
  readonly hint: string;
  readonly glyph: string;
  readonly primary?: boolean;
}): React.JSX.Element {
  // A `dl` group may only contain a `dt` followed by its `dd`, so the glyph, figure and hint all live
  // inside the `dd` rather than as siblings of it (axe rule: definition-list).
  return (
    <div className={`revenue-kpi${primary ? ' revenue-kpi--primary' : ''}`}>
      <dt className="revenue-kpi__label">{label}</dt>
      <dd className="revenue-kpi__body">
        <span className="revenue-kpi__glyph" aria-hidden="true">
          {glyph}
        </span>
        <span className="revenue-kpi__value">{value}</span>
        <span className="revenue-kpi__hint">{hint}</span>
      </dd>
    </div>
  );
}

/**
 * Horizontal capturable-versus-realized bars per play. Decorative reinforcement of the numbers,
 * which are printed on each row and repeated in full in the table equivalent.
 */
function PlayBars({ plays }: { readonly plays: readonly PipelinePlayLine[] }): React.JSX.Element {
  const max = plays.reduce((peak, play) => {
    const value = typeof play.capturable_cents === 'number' ? play.capturable_cents : 0;
    return Math.max(peak, value);
  }, 1);

  return (
    <ul className="play-bars">
      {plays.map((play) => {
        const capturable = typeof play.capturable_cents === 'number' ? play.capturable_cents : null;
        const realized = typeof play.realized_cents === 'number' ? play.realized_cents : null;
        // A masked band string cannot be drawn to scale, so the bar is simply omitted and the
        // figures still read from the row — never chart a value that is not a real number.
        const capturablePct = capturable === null ? null : Math.max(2, (capturable / max) * 100);
        const realizedPct =
          realized === null || capturable === null || capturable === 0
            ? null
            : Math.min(100, (realized / capturable) * 100);

        return (
          <li key={play.play} className="play-bars__row">
            <span className="play-bars__label">
              <span className="play-bars__glyph" aria-hidden="true">
                {PLAY_GLYPH[play.play]}
              </span>
              {PLAY_LABEL[play.play]}
            </span>
            <span className="play-bars__track">
              {capturablePct !== null && (
                <span className="play-bars__fill" style={{ width: `${String(capturablePct)}%` }}>
                  {realizedPct !== null && (
                    <span
                      className="play-bars__realized"
                      style={{ width: `${String(realizedPct)}%` }}
                    />
                  )}
                </span>
              )}
            </span>
            <span className="play-bars__value">{formatCompactCents(play.capturable_cents)}</span>
            <span className="play-bars__count">{play.opportunity_count} open</span>
          </li>
        );
      })}
      <li className="play-bars__legend">
        <span className="play-bars__legend-item">
          <span className="play-bars__swatch play-bars__swatch--capturable" aria-hidden="true" />
          Capturable
        </span>
        <span className="play-bars__legend-item">
          <span className="play-bars__swatch play-bars__swatch--realized" aria-hidden="true" />
          Realized
        </span>
      </li>
    </ul>
  );
}

/** The accessible equivalent of {@link PlayBars} — the same figures as a table (requirement 16.2). */
function PlayTable({ plays }: { readonly plays: readonly PipelinePlayLine[] }): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Opportunity by revenue play</caption>
      <thead>
        <tr>
          <th scope="col">Play</th>
          <th scope="col">Identified</th>
          <th scope="col">Capturable</th>
          <th scope="col">Realized</th>
          <th scope="col">Open opportunities</th>
        </tr>
      </thead>
      <tbody>
        {plays.map((play) => (
          <tr key={play.play}>
            <th scope="row">{PLAY_LABEL[play.play]}</th>
            <td>{formatCents(play.identified_cents)}</td>
            <td>{formatCents(play.capturable_cents)}</td>
            <td>{formatCents(play.realized_cents)}</td>
            <td>{play.opportunity_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * A decorative growth illustration: rising bars, a trend arc and a coin stack. Purely presentational
 * — the panel's numbers are all real text beside it, so this carries no information and is hidden
 * from assistive technology by its `aria-hidden` container.
 */
function GrowthIllustration(): React.JSX.Element {
  return (
    <svg viewBox="0 0 220 170" className="revenue-hero__svg" role="presentation">
      <defs>
        <linearGradient id="rev-bar" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor="rgba(255,255,255,0.25)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0.85)" />
        </linearGradient>
        <linearGradient id="rev-area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#ffe08a" stopOpacity="0.55" />
          <stop offset="100%" stopColor="#ffe08a" stopOpacity="0" />
        </linearGradient>
      </defs>

      {/* panel */}
      <rect
        x="14"
        y="18"
        width="150"
        height="118"
        rx="14"
        fill="rgba(255,255,255,0.10)"
        stroke="rgba(255,255,255,0.35)"
      />

      {/* baseline */}
      <line x1="30" y1="116" x2="150" y2="116" stroke="rgba(255,255,255,0.35)" strokeWidth="1.5" />

      {/* rising bars */}
      {[
        { x: 36, h: 26 },
        { x: 58, h: 40 },
        { x: 80, h: 34 },
        { x: 102, h: 58 },
        { x: 124, h: 76 },
      ].map((bar) => (
        <rect
          key={bar.x}
          x={bar.x}
          y={116 - bar.h}
          width="13"
          height={bar.h}
          rx="3.5"
          fill="url(#rev-bar)"
        />
      ))}

      {/* trend arc + area */}
      <path
        d="M 36 92 C 60 82, 74 70, 96 62 S 126 44, 142 32"
        fill="none"
        stroke="#ffe08a"
        strokeWidth="3"
        strokeLinecap="round"
      />
      <path
        d="M 36 92 C 60 82, 74 70, 96 62 S 126 44, 142 32 L 142 116 L 36 116 Z"
        fill="url(#rev-area)"
      />
      <circle cx="142" cy="32" r="5" fill="#ffe08a" />

      {/* coin stack */}
      <g>
        {[122, 132, 142].map((cy, i) => (
          <ellipse
            key={cy}
            cx="186"
            cy={cy}
            rx="22"
            ry="8"
            fill="rgba(255,255,255,0.9)"
            opacity={0.55 + i * 0.15}
          />
        ))}
        <text x="186" y="106" textAnchor="middle" fontSize="15" fill="#ffe08a">
          ✦
        </text>
      </g>
    </svg>
  );
}
