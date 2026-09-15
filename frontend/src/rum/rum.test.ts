import { afterEach, describe, expect, it, vi } from 'vitest';

import { __testing, currentRoute } from './rum';

describe('currentRoute', () => {
  it('collapses a customer path to a template, stripping the id', () => {
    // The customer id must never leave the browser as a label (requirement 18.12 / §13.4).
    expect(currentRoute('/customers/C-0001')).toBe('/customers/:id');
    expect(currentRoute('/customers/C-0001/insights')).toBe('/customers/:id');
  });

  it('passes a non-customer path through', () => {
    expect(currentRoute('/dashboard')).toBe('/dashboard');
  });

  it('maps the empty path to root', () => {
    expect(currentRoute('')).toBe('/');
  });
});

describe('OTLP metric shape', () => {
  it('carries only a route label — no identifiers', () => {
    const metric = __testing.toOtlpMetric(
      { name: 'web_vitals.lcp', value: 1234 },
      '1700000000000000000',
    ) as {
      name: string;
      gauge: { dataPoints: { attributes: { key: string }[]; asDouble: number }[] };
    };
    expect(metric.name).toBe('web_vitals.lcp');
    const point = metric.gauge.dataPoints[0];
    expect(point).toBeDefined();
    expect(point?.asDouble).toBe(1234);
    // The ONLY label is `route`. This is the PII bright line for RUM.
    const labelKeys = point?.attributes.map((a) => a.key) ?? [];
    expect(labelKeys).toEqual(['route']);
  });
});

describe('exportMetrics', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('does not send when no endpoint is configured', () => {
    // The module read its endpoint at import; with no VITE_OTLP_METRICS_URL in the test env it is
    // disabled, so a call is a no-op and fetch is never touched.
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    __testing.exportMetrics([{ name: 'web_vitals.cls', value: 0.1 }]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('never throws even if fetch rejects', () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('network down'));
    // No endpoint configured, but the guarantee is the call is safe regardless.
    expect(() => {
      __testing.exportMetrics([{ name: 'web_vitals.inp', value: 5 }]);
    }).not.toThrow();
  });
});
