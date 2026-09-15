import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, fetchHealth, fetchReadiness } from './client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api client', () => {
  it('requests /health and returns the envelope', async () => {
    // The parameter is declared so the recorded call arguments stay typed for the assertion below.
    const fetchMock = vi.fn((_input: string | URL | Request) =>
      Promise.resolve(
        jsonResponse({
          data: { status: 'ok', version: '0.1.0' },
          meta: {
            correlation_id: 'abc',
            trace_id: null,
            as_of: '2026-09-12T00:00:00Z',
            masked_fields: [],
            computed_fields: [],
            errors: [],
          },
        }),
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const envelope = await fetchHealth();

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/health');
    expect(envelope.data.status).toBe('ok');
    expect(envelope.meta.correlation_id).toBe('abc');
  });

  it('throws ApiError carrying the code and correlation ID', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'CUSTOMER_NOT_FOUND',
                message: 'Not Found',
                correlation_id: 'req-42',
              },
            },
            404,
          ),
        ),
      ),
    );

    const error = await fetchHealth().catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(404);
    expect(apiError.code).toBe('CUSTOMER_NOT_FOUND');
    expect(apiError.correlationId).toBe('req-42');
  });

  it('resolves a 503 readiness envelope instead of throwing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            {
              data: { status: 'not_ready', version: '0.1.0', components: [] },
              meta: {
                correlation_id: 'abc',
                trace_id: null,
                as_of: '2026-09-12T00:00:00Z',
                masked_fields: [],
                computed_fields: [],
                errors: [],
              },
            },
            503,
          ),
        ),
      ),
    );

    const envelope = await fetchReadiness();

    expect(envelope.data.status).toBe('not_ready');
  });

  it('raises INTERNAL_ERROR when the body is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('<html>502</html>', { status: 502 }))),
    );

    const error = (await fetchHealth().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe('INTERNAL_ERROR');
    expect(error.status).toBe(502);
  });
});
