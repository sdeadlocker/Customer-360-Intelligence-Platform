import { useMemo, useState } from 'react';

import { ModuleState, type ModuleStateModel } from '../../../components/ModuleState';
import { formatDate, humanizeEnum } from './format';
import type { EngagementData, EngagementEventData } from './types';
import { useSubResource } from './useSubResource';

/** A decorative glyph per channel (presentational only). */
function channelGlyph(channel: string): string {
  const key = channel.toUpperCase();
  if (key.includes('BRANCH')) return '🏦';
  if (key.includes('MOBILE') || key.includes('APP')) return '📱';
  if (key.includes('WEB') || key.includes('ONLINE')) return '💻';
  if (key.includes('PHONE') || key.includes('CALL') || key.includes('IVR')) return '📞';
  if (key.includes('EMAIL')) return '✉️';
  if (key.includes('ATM')) return '🏧';
  return '📡';
}

/** Outcome → a tone + glyph so a result reads at a glance (colour never stands alone). */
function outcomeTone(outcome: string | null | undefined): { tone: string; glyph: string } {
  const key = (outcome ?? '').toUpperCase();
  if (key.includes('SUCCESS') || key.includes('COMPLETE') || key.includes('APPROVED')) {
    return { tone: 'pass', glyph: '✓' };
  }
  if (key.includes('FAIL') || key.includes('DECLIN') || key.includes('ERROR')) {
    return { tone: 'fail', glyph: '✕' };
  }
  if (key === '') {
    return { tone: 'muted', glyph: '•' };
  }
  return { tone: 'info', glyph: '•' };
}

/** Whether an outcome counts as a success, for the success-rate KPI. */
function isSuccess(outcome: string | null | undefined): boolean {
  return outcomeTone(outcome).tone === 'pass';
}

/**
 * Engagement history widget (task 13.8, requirement 7.6).
 *
 * Engagement events across channels (login, OTP, password reset, branch visit, app usage, …),
 * newest first, filterable by channel and event type. Every event field the backend returns is
 * displayed. The list is not part of the composed 360 payload, so it is fetched from `/engagement`;
 * the filters here are client-side over the fetched page (the endpoint also accepts server-side
 * `channel`/`event_type` filters, used by the Phase 14 ask-anything path).
 */
export function EngagementWidget({
  customerId,
}: {
  readonly customerId: string;
}): React.JSX.Element {
  const engagement = useSubResource<EngagementData>(
    `/customers/${encodeURIComponent(customerId)}/engagement`,
  );

  const model: ModuleStateModel =
    engagement.kind === 'loading'
      ? { status: 'loading' }
      : engagement.kind === 'error'
        ? {
            status: 'error',
            errorMessage: engagement.message,
            correlationId: engagement.correlationId,
          }
        : { status: 'ready' };

  const events = engagement.kind === 'ready' ? (engagement.data.events ?? []) : [];
  const isEmpty = engagement.kind === 'ready' && events.length === 0;

  return (
    <ModuleState title="Engagement history" state={model} isEmpty={isEmpty}>
      {engagement.kind === 'ready' && events.length > 0 && <EngagementTable events={events} />}
    </ModuleState>
  );
}

function EngagementTable({
  events,
}: {
  readonly events: readonly EngagementEventData[];
}): React.JSX.Element {
  const [channel, setChannel] = useState<string>('');
  const [eventType, setEventType] = useState<string>('');

  const channels = useMemo(() => unique(events.map((e) => e.channel)), [events]);
  const types = useMemo(() => unique(events.map((e) => e.event_type)), [events]);

  // KPI summary + per-channel distribution over the whole fetched set.
  const successCount = useMemo(() => events.filter((e) => isSuccess(e.outcome)).length, [events]);
  const successRate = events.length > 0 ? Math.round((successCount / events.length) * 100) : 0;
  const byChannel = useMemo(() => {
    const map = new Map<string, number>();
    for (const e of events) {
      map.set(e.channel, (map.get(e.channel) ?? 0) + 1);
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }, [events]);
  const maxChannel = Math.max(1, ...byChannel.map(([, n]) => n));

  const filtered = useMemo(
    () =>
      events.filter(
        (e) =>
          (channel === '' || e.channel === channel) &&
          (eventType === '' || e.event_type === eventType),
      ),
    [events, channel, eventType],
  );

  return (
    <div className="widget">
      {/* KPI tiles: total events, success rate, and channels used. */}
      <div className="engagement__kpis">
        <EngagementKpi icon="📊" tone="indigo" value={String(events.length)} label="Total events" />
        <EngagementKpi
          icon="✅"
          tone="green"
          value={`${String(successRate)}%`}
          label="Success rate"
        />
        <EngagementKpi
          icon="📡"
          tone="violet"
          value={String(channels.length)}
          label="Channels used"
        />
      </div>

      {/* Per-channel distribution mini bar chart (the table below carries the same data as text). */}
      {byChannel.length > 0 && (
        <div className="engagement__channels" aria-hidden="true">
          {byChannel.map(([ch, n]) => (
            <div key={ch} className="engagement-bar">
              <span className="engagement-bar__label">
                <span className="engagement-bar__glyph">{channelGlyph(ch)}</span>
                {humanizeEnum(ch)}
              </span>
              <span className="engagement-bar__track">
                <span
                  className="engagement-bar__fill"
                  style={{ width: `${String(Math.max(4, (n / maxChannel) * 100))}%` }}
                />
              </span>
              <span className="engagement-bar__value">{n}</span>
            </div>
          ))}
        </div>
      )}

      <div className="engagement__filters">
        <label className="filter-select">
          <span>Channel</span>
          <select
            className="focus-ring"
            value={channel}
            onChange={(event) => {
              setChannel(event.target.value);
            }}
          >
            <option value="">All channels</option>
            {channels.map((c) => (
              <option key={c} value={c}>
                {humanizeEnum(c)}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-select">
          <span>Event type</span>
          <select
            className="focus-ring"
            value={eventType}
            onChange={(event) => {
              setEventType(event.target.value);
            }}
          >
            <option value="">All types</option>
            {types.map((t) => (
              <option key={t} value={t}>
                {humanizeEnum(t)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {filtered.length === 0 ? (
        <p className="module__empty">No events match the selected filters.</p>
      ) : (
        <div
          className="table-scroll table-scroll--capped"
          tabIndex={0}
          aria-label="Engagement events, scrollable"
        >
          <table className="data-table">
            <caption>Engagement events</caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Event</th>
                <th scope="col">Channel</th>
                <th scope="col">Device</th>
                <th scope="col">Outcome</th>
                <th scope="col">Notes</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((event) => {
                const oc = outcomeTone(event.outcome);
                return (
                  <tr key={event.event_id}>
                    <th scope="row">{formatDate(event.event_date)}</th>
                    <td>{humanizeEnum(event.event_type)}</td>
                    <td>
                      <span className="engagement__channel">
                        <span aria-hidden="true">{channelGlyph(event.channel)}</span>
                        {humanizeEnum(event.channel)}
                      </span>
                    </td>
                    <td>{event.device_type ?? '—'}</td>
                    <td>
                      <span className={`engagement__outcome engagement__outcome--${oc.tone}`}>
                        <span aria-hidden="true" className="engagement__outcome-glyph">
                          {oc.glyph}
                        </span>
                        {humanizeEnum(event.outcome)}
                      </span>
                    </td>
                    <td>{event.notes ?? '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function EngagementKpi({
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
    <div className={`engagement-kpi engagement-kpi--${tone}`}>
      <span className="engagement-kpi__icon" aria-hidden="true">
        {icon}
      </span>
      <span className="engagement-kpi__body">
        <span className="engagement-kpi__value">{value}</span>
        <span className="engagement-kpi__label">{label}</span>
      </span>
    </div>
  );
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)].sort();
}
