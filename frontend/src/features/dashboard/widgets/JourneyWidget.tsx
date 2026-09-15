import { useMemo, useState } from 'react';

import { ModuleState } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';
import { useFilters } from '../filters';
import { formatCents, formatDate } from './format';
import { pick, type TimelineData, type TimelineEntryData } from './types';
import { deriveWidgetState } from './widgetState';

/**
 * Customer journey timeline widget (task 13.6, requirements 7.1–7.5, 7.7).
 *
 * A merged, chronological timeline across the customer's tenure — life events, applications, major
 * transactions and relationship changes — laid out on a horizontal axis the user can zoom and pan.
 * Selecting an entry opens a milestone detail panel (requirement 7.7). The axis honours the
 * dashboard date-range filter (requirement 3.7).
 *
 * Note on inference labelling: the composed timeline the backend serves carries a category, date,
 * title and source reference, but not the per-event inference confidence or signals — those live on
 * the life-event model and are surfaced by the Phase 14 AI experience (the life-event agent), not by
 * this deterministic widget. Entry categories are therefore shown by type, without an AI-confidence
 * badge this layer has no data to populate.
 */

const CATEGORY_LABELS: Record<TimelineEntryData['category'], string> = {
  LIFE_EVENT: 'Life event',
  APPLICATION: 'Application',
  MAJOR_TRANSACTION: 'Major transaction',
  RELATIONSHIP_CHANGE: 'Relationship change',
};

/** A decorative glyph per timeline category, shown on the timeline node and in summaries. */
const CATEGORY_GLYPH: Record<TimelineEntryData['category'], string> = {
  LIFE_EVENT: '🎉',
  APPLICATION: '📝',
  MAJOR_TRANSACTION: '💳',
  RELATIONSHIP_CHANGE: '🔗',
};

const CATEGORY_ORDER: readonly TimelineEntryData['category'][] = [
  'LIFE_EVENT',
  'APPLICATION',
  'MAJOR_TRANSACTION',
  'RELATIONSHIP_CHANGE',
];

/** Three zoom levels control the timeline's vertical density (compact ↔ roomy). */
const ZOOM_LEVELS = ['compact', 'normal', 'roomy'] as const;
type Zoom = (typeof ZOOM_LEVELS)[number];

export function JourneyWidget({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const { filters } = useFilters();
  const state = deriveWidgetState(view, { module: 'journey', slots: ['timeline'] });
  const timeline = pick<TimelineData>(view.raw, 'timeline');

  const entries = useMemo(() => {
    const all = timeline?.timeline ?? [];
    return all
      .filter((entry) =>
        withinRange(entry.entry_date, filters.dateRange.from, filters.dateRange.to),
      )
      .slice()
      .sort((a, b) => a.entry_date.localeCompare(b.entry_date));
  }, [timeline, filters.dateRange.from, filters.dateRange.to]);

  const [selected, setSelected] = useState<TimelineEntryData | null>(null);
  const [zoom, setZoom] = useState<Zoom>('normal');

  // Counts per category, for the summary chip row.
  const counts = useMemo(() => {
    const map = new Map<TimelineEntryData['category'], number>();
    for (const entry of entries) {
      map.set(entry.category, (map.get(entry.category) ?? 0) + 1);
    }
    return map;
  }, [entries]);

  const zoomIndex = ZOOM_LEVELS.indexOf(zoom);

  const isEmpty = state.isEmpty || entries.length === 0;

  return (
    <ModuleState title="Journey" state={state.model} isEmpty={isEmpty}>
      {timeline !== undefined && entries.length > 0 && (
        <div className="widget">
          {/* Category summary chips + zoom controls form a small toolbar over the timeline. */}
          <div className="journey__toolbar">
            <div className="journey__summary" aria-hidden="true">
              {CATEGORY_ORDER.filter((c) => (counts.get(c) ?? 0) > 0).map((category) => (
                <span
                  key={category}
                  className={`journey-chip journey-chip--${category.toLowerCase()}`}
                >
                  <span className="journey-chip__glyph">{CATEGORY_GLYPH[category]}</span>
                  <span className="journey-chip__count">{counts.get(category)}</span>
                  <span className="journey-chip__label">{CATEGORY_LABELS[category]}</span>
                </span>
              ))}
            </div>
            <div className="journey__zoom" role="group" aria-label="Timeline zoom">
              <button
                type="button"
                className="button button--subtle focus-ring"
                aria-label="Zoom out"
                disabled={zoomIndex <= 0}
                onClick={() => {
                  setZoom(ZOOM_LEVELS[Math.max(0, zoomIndex - 1)] ?? 'normal');
                }}
              >
                −
              </button>
              <button
                type="button"
                className="button button--subtle focus-ring"
                aria-label="Zoom in"
                disabled={zoomIndex >= ZOOM_LEVELS.length - 1}
                onClick={() => {
                  setZoom(ZOOM_LEVELS[Math.min(ZOOM_LEVELS.length - 1, zoomIndex + 1)] ?? 'normal');
                }}
              >
                +
              </button>
            </div>
          </div>

          <ol
            className={`timeline-v timeline-v--${zoom}`}
            aria-label="Journey timeline, most recent last"
            tabIndex={0}
          >
            {entries.map((entry) => {
              const active = selected?.source_id === entry.source_id;
              return (
                <li key={`${entry.category}-${entry.source_id}`} className="timeline-v__item">
                  <button
                    type="button"
                    className={`timeline-v__row timeline-v__row--${entry.category.toLowerCase()} focus-ring${active ? ' timeline-v__row--active' : ''}`}
                    onClick={() => {
                      setSelected((cur) => (cur?.source_id === entry.source_id ? null : entry));
                    }}
                    aria-label={`${CATEGORY_LABELS[entry.category]}: ${entry.title}, ${formatDate(entry.entry_date)}`}
                  >
                    <span
                      className={`timeline-v__node timeline-v__node--${entry.category.toLowerCase()}`}
                      aria-hidden="true"
                    >
                      {CATEGORY_GLYPH[entry.category]}
                    </span>
                    <span className="timeline-v__date">{formatDate(entry.entry_date)}</span>
                    <span className="timeline-v__body">
                      <span className="timeline-v__title">{entry.title}</span>
                      <span className="timeline-v__meta">
                        <span className={`badge badge--cat-${entry.category.toLowerCase()}`}>
                          {CATEGORY_LABELS[entry.category]}
                        </span>
                        {entry.amount_cents != null && (
                          <span className="timeline-v__amount">
                            {formatCents(entry.amount_cents)}
                          </span>
                        )}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>

          {selected !== null && <MilestoneDetail entry={selected} />}
        </div>
      )}
    </ModuleState>
  );
}

function MilestoneDetail({ entry }: { readonly entry: TimelineEntryData }): React.JSX.Element {
  return (
    <div className="milestone" role="group" aria-label="Milestone detail">
      <div className="milestone__head">
        <span
          className={`milestone__glyph milestone__glyph--${entry.category.toLowerCase()}`}
          aria-hidden="true"
        >
          {CATEGORY_GLYPH[entry.category]}
        </span>
        <h3 className="milestone__title">{entry.title}</h3>
      </div>
      <dl className="kv">
        <dt className="kv__key">Type</dt>
        <dd className="kv__val">
          <span className={`badge badge--cat-${entry.category.toLowerCase()}`}>
            {CATEGORY_LABELS[entry.category]}
          </span>
        </dd>
        <dt className="kv__key">Date</dt>
        <dd className="kv__val">{formatDate(entry.entry_date)}</dd>
        {entry.amount_cents != null && (
          <>
            <dt className="kv__key">Amount</dt>
            <dd className="kv__val">{formatCents(entry.amount_cents)}</dd>
          </>
        )}
        <dt className="kv__key">Reference</dt>
        <dd className="kv__val mono">{entry.source_id}</dd>
      </dl>
    </div>
  );
}

function withinRange(dateIso: string, from: string | null, to: string | null): boolean {
  if (from !== null && dateIso < from) {
    return false;
  }
  if (to !== null && dateIso > to) {
    return false;
  }
  return true;
}
