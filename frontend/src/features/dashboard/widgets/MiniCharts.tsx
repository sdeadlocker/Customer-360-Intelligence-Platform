/**
 * Presentational mini-charts (visual enhancement only).
 *
 * These components render *existing, already-computed* data as richer visuals — a composition donut
 * and a smooth line chart — without fetching, deriving, or changing any business data. They are
 * decorative companions to the numbers and tables that already carry the same values, so they are
 * marked `aria-hidden`: assistive technology continues to read the metric tiles, the net-worth
 * breakdown, and the ChartWithTable data tables, which remain the source of truth. Nothing here
 * alters widget logic, state, or the values shown elsewhere.
 */

/** A colour ramp reused across the mini-charts, matching the dashboard's fintech palette. */
const SERIES_COLORS = ['#6366f1', '#0ea5e9', '#14b8a6', '#f97316', '#ec4899', '#a855f7'] as const;

export interface DonutSlice {
  readonly label: string;
  readonly value: number;
  readonly color: string;
}

/**
 * A colourful composition donut with a side legend. Values are absolute magnitudes (already known
 * numbers); slices below a hair of the total are still drawn so the legend and ring agree.
 */
export function CompositionDonut({
  slices,
  centerLabel,
  centerValue,
}: {
  readonly slices: readonly DonutSlice[];
  readonly centerLabel: string;
  readonly centerValue: string;
}): React.JSX.Element {
  const total = slices.reduce((sum, s) => sum + Math.max(0, s.value), 0);
  const radius = 52;
  const circumference = 2 * Math.PI * radius;

  // Build stacked arc segments in a plain loop (no post-render mutation): each arc starts where the
  // previous one ended by carrying a running offset in a local `built` array.
  const arcs = buildArcs(slices, total, circumference);

  return (
    <div className="mini-donut" aria-hidden="true">
      <svg viewBox="0 0 140 140" className="mini-donut__svg" role="presentation">
        <circle
          cx="70"
          cy="70"
          r={radius}
          fill="none"
          stroke="var(--color-surface-raised)"
          strokeWidth="16"
        />
        {arcs.map((arc, i) => (
          <circle
            key={i}
            cx="70"
            cy="70"
            r={radius}
            fill="none"
            stroke={arc.color}
            strokeWidth="16"
            strokeDasharray={arc.dash}
            strokeLinecap="butt"
            transform={`rotate(${String(arc.rotation)} 70 70)`}
          />
        ))}
        <text x="70" y="64" textAnchor="middle" className="mini-donut__center-value">
          {centerValue}
        </text>
        <text x="70" y="82" textAnchor="middle" className="mini-donut__center-label">
          {centerLabel}
        </text>
      </svg>
      <ul className="mini-donut__legend">
        {slices.map((slice) => (
          <li key={slice.label} className="mini-donut__legend-item">
            <span className="mini-donut__swatch" style={{ background: slice.color }} />
            <span className="mini-donut__legend-label">{slice.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * A smooth (Catmull-Rom → cubic Bézier) multi-point line chart with a soft gradient area fill and
 * dot markers. `points` are y-values already scaled by the caller's data; `flags` marks emphasised
 * markers (e.g. anomalies). Purely decorative — the accessible table sits beside it.
 */
export function SmoothLineChart({
  values,
  labels,
  flags = [],
  colorId = 'a',
}: {
  readonly values: readonly number[];
  readonly labels: readonly string[];
  readonly flags?: readonly boolean[];
  readonly colorId?: string;
}): React.JSX.Element {
  const width = 320;
  const height = 140;
  const padX = 14;
  const padY = 18;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;

  const coords = values.map((v, i) => {
    const x =
      values.length <= 1 ? width / 2 : padX + (i / (values.length - 1)) * (width - 2 * padX);
    const y = height - padY - ((v - min) / span) * (height - 2 * padY);
    return { x, y };
  });

  const line = smoothPath(coords);
  const firstPt = coords[0];
  const lastPt = coords[coords.length - 1];
  const area =
    firstPt !== undefined && lastPt !== undefined
      ? `${line} L ${String(lastPt.x)} ${String(height - padY)} L ${String(firstPt.x)} ${String(height - padY)} Z`
      : '';

  const stroke = SERIES_COLORS[0];
  const gradId = `mini-line-grad-${colorId}`;

  return (
    <div className="mini-line" aria-hidden="true">
      <svg
        viewBox={`0 0 ${String(width)} ${String(height)}`}
        className="mini-line__svg"
        role="presentation"
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.28" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        {/* faint baselines */}
        {[0.25, 0.5, 0.75].map((f) => (
          <line
            key={f}
            x1={padX}
            x2={width - padX}
            y1={padY + f * (height - 2 * padY)}
            y2={padY + f * (height - 2 * padY)}
            stroke="var(--color-border)"
            strokeWidth="0.6"
            strokeDasharray="3 3"
          />
        ))}
        {area !== '' && <path d={area} fill={`url(#${gradId})`} />}
        {line !== '' && (
          <path
            d={line}
            fill="none"
            stroke={stroke}
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        )}
        {coords.map((c, i) => {
          const flagged = flags[i] === true;
          return (
            <circle
              key={i}
              cx={c.x}
              cy={c.y}
              r={flagged ? 4 : 2.6}
              fill={flagged ? '#f97316' : '#fff'}
              stroke={flagged ? '#f97316' : stroke}
              strokeWidth="2"
            />
          );
        })}
      </svg>
      <ul className="mini-line__axis">
        {labels.map((label, i) => (
          <li key={`${label}-${String(i)}`} className="mini-line__tick">
            {label}
          </li>
        ))}
      </ul>
    </div>
  );
}

interface Arc {
  readonly color: string;
  readonly dash: string;
  readonly rotation: number;
}

/** Stack donut arcs around the ring without mutating anything after render. */
function buildArcs(
  slices: readonly DonutSlice[],
  total: number,
  circumference: number,
): readonly Arc[] {
  const out: Arc[] = [];
  let offset = 0;
  for (const slice of slices) {
    const fraction = total > 0 ? Math.max(0, slice.value) / total : 0;
    const length = fraction * circumference;
    out.push({
      color: slice.color,
      dash: `${String(length)} ${String(circumference - length)}`,
      rotation: (offset / circumference) * 360 - 90,
    });
    offset += length;
  }
  return out;
}

interface Pt {
  readonly x: number;
  readonly y: number;
}

/** Build a smooth cubic path through the given points using a Catmull-Rom spline. */
function smoothPath(pts: readonly Pt[]): string {
  const first = pts[0];
  if (first === undefined) {
    return '';
  }
  if (pts.length === 1) {
    return `M ${String(first.x)} ${String(first.y)}`;
  }
  let d = `M ${String(first.x)} ${String(first.y)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p1 = pts[i];
    const p2 = pts[i + 1];
    if (p1 === undefined || p2 === undefined) {
      continue;
    }
    const p0 = pts[i - 1] ?? p1;
    const p3 = pts[i + 2] ?? p2;
    const c1x = p1.x + (p2.x - p0.x) / 6;
    const c1y = p1.y + (p2.y - p0.y) / 6;
    const c2x = p2.x - (p3.x - p1.x) / 6;
    const c2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C ${String(c1x)} ${String(c1y)}, ${String(c2x)} ${String(c2y)}, ${String(p2.x)} ${String(p2.y)}`;
  }
  return d;
}
