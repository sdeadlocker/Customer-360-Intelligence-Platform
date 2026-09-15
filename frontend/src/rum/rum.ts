/**
 * Real user monitoring (task 10.5, requirement 18.12).
 *
 * The browser reports two things over OTLP: Google's Web Vitals (LCP, INP, CLS — the loading,
 * interactivity and stability signals) and two application marks the design names —
 * `dashboard.meaningful_render` and `insights.first_card`. Both go out as OTLP/HTTP JSON to the
 * same collector the backend exports to, so the frontend and backend share one pipeline.
 *
 * Two rules shape this module, mirroring the backend's telemetry discipline:
 *
 * - **No PII, ever.** A RUM signal carries a metric name, a numeric value and a coarse page label
 *   (`route`), and nothing else. The route is the *templated* path (`/customers/:id`), never the
 *   real URL — a raw path carries the customer id, exactly as the backend access log withholds it.
 *   No query string, no element text, no identifiers.
 * - **Best-effort and non-blocking.** RUM must never affect the page. Every send is fire-and-forget
 *   with `keepalive`, failures are swallowed, and a missing endpoint disables reporting entirely.
 */

import { onCLS, onINP, onLCP, type Metric } from 'web-vitals';

/** The OTLP/HTTP metrics endpoint. Empty (the default) disables RUM — a build with no collector. */
const OTLP_METRICS_URL: string = import.meta.env.VITE_OTLP_METRICS_URL ?? '';

/** Resource attributes identifying the browser app, matching the backend's resource shape. */
const RESOURCE_ATTRIBUTES = [
  { key: 'service.name', value: { stringValue: 'c360-web' } },
  { key: 'service.namespace', value: { stringValue: 'c360' } },
  { key: 'telemetry.sdk.language', value: { stringValue: 'webjs' } },
] as const;

/**
 * The templated route for the current page. The app is a single dashboard today, so this collapses
 * the customer-scoped path to its template and everything else to the pathname's first segment.
 * Crucially it strips any customer id: `/customers/C-0001` becomes `/customers/:id`.
 */
export function currentRoute(pathname: string = window.location.pathname): string {
  const customerMatch = /^\/customers\/[^/]+/.exec(pathname);
  if (customerMatch !== null) {
    return '/customers/:id';
  }
  return pathname === '' ? '/' : pathname;
}

interface DataPoint {
  readonly value: number;
  readonly name: string;
}

/** Build one OTLP metric from a name and a value, labelled only with the templated route. */
function toOtlpMetric(point: DataPoint, timeUnixNano: string): unknown {
  return {
    name: point.name,
    unit: 'ms',
    gauge: {
      dataPoints: [
        {
          asDouble: point.value,
          timeUnixNano,
          attributes: [{ key: 'route', value: { stringValue: currentRoute() } }],
        },
      ],
    },
  };
}

/** POST a batch of metrics as OTLP/HTTP JSON. Fire-and-forget; never throws. */
function exportMetrics(points: readonly DataPoint[]): void {
  if (OTLP_METRICS_URL === '' || points.length === 0) {
    return;
  }
  const timeUnixNano = `${Date.now() * 1_000_000}`;
  const body = JSON.stringify({
    resourceMetrics: [
      {
        resource: { attributes: RESOURCE_ATTRIBUTES },
        scopeMetrics: [
          {
            scope: { name: 'c360.web.rum' },
            metrics: points.map((point) => toOtlpMetric(point, timeUnixNano)),
          },
        ],
      },
    ],
  });

  try {
    void fetch(OTLP_METRICS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).catch(() => {
      /* RUM is best-effort: a failed send must never surface to the user. */
    });
  } catch {
    /* `fetch` can throw synchronously on a malformed URL; swallow it. */
  }
}

/** Web Vitals handler: forwards one vital as a gauge named `web_vitals.<lcp|inp|cls>`. */
function reportVital(metric: Metric): void {
  exportMetrics([{ name: `web_vitals.${metric.name.toLowerCase()}`, value: metric.value }]);
}

/**
 * Emit a custom application mark. The design names two — `dashboard.meaningful_render` and
 * `insights.first_card` — but any mark is accepted; the value is the time since navigation start.
 */
export function mark(name: 'dashboard.meaningful_render' | 'insights.first_card'): void {
  const value =
    typeof performance !== 'undefined' && typeof performance.now === 'function'
      ? performance.now()
      : 0;
  exportMetrics([{ name, value }]);
}

/** Whether RUM is configured to report. Exposed so tests and callers can branch without env peeking. */
export function rumEnabled(): boolean {
  return OTLP_METRICS_URL !== '';
}

/** Register the Web Vitals listeners. Idempotent-safe to call once at startup. */
export function initRum(): void {
  if (!rumEnabled()) {
    return;
  }
  onLCP(reportVital);
  onINP(reportVital);
  onCLS(reportVital);
}

// Exposed for testing the exporter shape without a live collector.
export const __testing = { toOtlpMetric, exportMetrics };
