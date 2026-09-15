import createClient, { type Middleware } from 'openapi-fetch';

import { tokenStore } from '../auth/tokenStore';
import type { paths } from './generated/schema';
import type {
  CustomerSearchHit,
  Envelope,
  ErrorEnvelope,
  HealthPayload,
  MeResponse,
  Page,
  ReadyPayload,
  TokenPayload,
} from './types';

/**
 * A coded API failure.
 *
 * The correlation ID is carried through to the UI on purpose: it is the one value that lets a user
 * report an error and an engineer find the exact trace behind it (design §6.3, requirement 18.2).
 */
export class ApiError extends Error {
  readonly code: string;
  readonly correlationId: string | null;
  readonly status: number;

  constructor(message: string, status: number, code: string, correlationId: string | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
  }
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== 'object' || value === null || !('error' in value)) {
    return false;
  }
  const { error } = value;
  return typeof error === 'object' && error !== null && 'code' in error && 'message' in error;
}

// ---------------------------------------------------------------- refresh coordination
// A single in-flight refresh is shared by every 401'd request, so a burst of concurrent calls after
// an access token expires triggers exactly one refresh rather than a thundering herd.
let refreshInFlight: Promise<boolean> | null = null;

/** Called when authentication is irrecoverably lost, so the app can route back to login. */
let onAuthLost: (() => void) | null = null;
export function setAuthLostHandler(handler: (() => void) | null): void {
  onAuthLost = handler;
}

async function refreshTokens(): Promise<boolean> {
  const tokens = tokenStore.get();
  if (tokens === null) {
    return false;
  }
  try {
    const response = await fetch('/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ refresh_token: tokens.refreshToken }),
    });
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok || !isEnvelope<TokenPayload>(body)) {
      return false;
    }
    tokenStore.set({
      accessToken: body.data.access_token,
      refreshToken: body.data.refresh_token,
    });
    return true;
  } catch {
    return false;
  }
}

function isEnvelope<TData>(value: unknown): value is Envelope<TData> {
  return typeof value === 'object' && value !== null && 'data' in value && 'meta' in value;
}

// ---------------------------------------------------------------- typed client
// The generated `paths` type makes every request path, method, query and response body checked at
// compile time: a call to an endpoint the backend does not publish, or reading a field the response
// does not carry, is a type error — the drift the task requires to fail the build.
const raw = createClient<paths>();

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const tokens = tokenStore.get();
    if (tokens !== null) {
      request.headers.set('Authorization', `Bearer ${tokens.accessToken}`);
    }
    request.headers.set('Accept', 'application/json');
    return request;
  },
};

raw.use(authMiddleware);

/**
 * Issue a typed GET, attaching the bearer token, transparently refreshing once on a 401, and
 * converting a coded error body into an `ApiError`.
 *
 * A 401 that survives a refresh means the session is gone: the stored tokens are cleared and the
 * auth-lost handler (wired by the auth provider) routes the user back to login. Only the *first*
 * 401 in a request's life triggers a refresh; a second 401 after refreshing is treated as terminal
 * so a broken token cannot loop.
 */
async function request<TData>(
  path: string,
  init: { signal?: AbortSignal } = {},
  retried = false,
): Promise<Envelope<TData>> {
  const response = await fetch(path, {
    headers: authHeaders(),
    ...(init.signal ? { signal: init.signal } : {}),
  });

  if (response.status === 401 && !retried) {
    // Coalesce concurrent refreshes into one.
    refreshInFlight ??= refreshTokens().finally(() => {
      refreshInFlight = null;
    });
    const refreshed = await refreshInFlight;
    if (refreshed) {
      return request<TData>(path, init, true);
    }
    tokenStore.clear();
    onAuthLost?.();
  }

  const body: unknown = await response.json().catch(() => null);

  if (isErrorEnvelope(body)) {
    throw new ApiError(
      body.error.message,
      response.status,
      String(body.error.code),
      body.error.correlation_id,
    );
  }
  if (body === null) {
    throw new ApiError(`Malformed response from ${path}`, response.status, 'INTERNAL_ERROR', null);
  }
  return body as Envelope<TData>;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const tokens = tokenStore.get();
  if (tokens !== null) {
    headers.Authorization = `Bearer ${tokens.accessToken}`;
  }
  return headers;
}

// ---------------------------------------------------------------- public API
export function fetchHealth(signal?: AbortSignal): Promise<Envelope<HealthPayload>> {
  return request<HealthPayload>('/health', signal ? { signal } : {});
}

export function fetchReadiness(signal?: AbortSignal): Promise<Envelope<ReadyPayload>> {
  return request<ReadyPayload>('/ready', signal ? { signal } : {});
}

export function fetchMe(signal?: AbortSignal): Promise<Envelope<MeResponse>> {
  return request<MeResponse>('/me', signal ? { signal } : {});
}

export interface SearchParams {
  readonly q: string;
  readonly limit?: number;
  readonly cursor?: string | null;
}

export function searchCustomers(
  params: SearchParams,
  signal?: AbortSignal,
): Promise<Envelope<Page<CustomerSearchHit>>> {
  const query = new URLSearchParams({ q: params.q });
  if (params.limit !== undefined) {
    query.set('limit', String(params.limit));
  }
  if (params.cursor != null && params.cursor !== '') {
    query.set('cursor', params.cursor);
  }
  return request<Page<CustomerSearchHit>>(
    `/customers?${query.toString()}`,
    signal ? { signal } : {},
  );
}

/** Password grant. On success, tokens are stored and subsequent requests are authenticated. */
export async function login(username: string, password: string): Promise<void> {
  const response = await fetch('/auth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const body: unknown = await response.json().catch(() => null);

  if (isErrorEnvelope(body)) {
    throw new ApiError(
      body.error.message,
      response.status,
      String(body.error.code),
      body.error.correlation_id,
    );
  }
  if (!response.ok || !isEnvelope<TokenPayload>(body)) {
    throw new ApiError('Login failed', response.status, 'UNAUTHENTICATED', null);
  }
  tokenStore.set({
    accessToken: body.data.access_token,
    refreshToken: body.data.refresh_token,
  });
}

export function logout(): void {
  tokenStore.clear();
}

/**
 * Authenticated GET of an arbitrary envelope path, with the same token-injection and refresh-on-401
 * behaviour as the named helpers. Used by feature modules (the 360 read model, later widgets) that
 * fetch a path whose payload is `dict[str, Any]` on the wire and is refined in the feature itself.
 */
export function getEnvelope<TData>(path: string, signal?: AbortSignal): Promise<Envelope<TData>> {
  return request<TData>(path, signal ? { signal } : {});
}

/**
 * Authenticated POST of an arbitrary envelope path with an empty body, sharing the token-injection,
 * refresh-on-401 and coded-error handling of the GET helper. Used by the signals worklist for the
 * dismiss/ack state writes (Phase 17), which take no request body.
 */
export async function postEnvelope<TData>(
  path: string,
  signal?: AbortSignal,
): Promise<Envelope<TData>> {
  const response = await fetch(path, {
    method: 'POST',
    headers: authHeaders(),
    ...(signal ? { signal } : {}),
  });
  const body: unknown = await response.json().catch(() => null);
  if (isErrorEnvelope(body)) {
    throw new ApiError(
      body.error.message,
      response.status,
      String(body.error.code),
      body.error.correlation_id,
    );
  }
  if (body === null) {
    throw new ApiError(`Malformed response from ${path}`, response.status, 'INTERNAL_ERROR', null);
  }
  return body as Envelope<TData>;
}

/**
 * Authenticated POST of an arbitrary envelope path with a JSON body, sharing the token-injection,
 * refresh-on-401 and coded-error handling of the other helpers. Used by the report feature (Phase
 * 18) to define reports and attach schedules, both of which carry a JSON body.
 */
export async function postJsonEnvelope<TData>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<Envelope<TData>> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    ...(signal ? { signal } : {}),
  });
  const parsed: unknown = await response.json().catch(() => null);
  if (isErrorEnvelope(parsed)) {
    throw new ApiError(
      parsed.error.message,
      response.status,
      String(parsed.error.code),
      parsed.error.correlation_id,
    );
  }
  if (parsed === null) {
    throw new ApiError(`Malformed response from ${path}`, response.status, 'INTERNAL_ERROR', null);
  }
  return parsed as Envelope<TData>;
}

/**
 * Authenticated DELETE of an arbitrary envelope path, sharing the token-injection, refresh-on-401
 * and coded-error handling. Used by the report feature to disable a schedule (Phase 18).
 */
export async function deleteEnvelope<TData>(
  path: string,
  signal?: AbortSignal,
): Promise<Envelope<TData>> {
  const response = await fetch(path, {
    method: 'DELETE',
    headers: authHeaders(),
    ...(signal ? { signal } : {}),
  });
  const parsed: unknown = await response.json().catch(() => null);
  if (isErrorEnvelope(parsed)) {
    throw new ApiError(
      parsed.error.message,
      response.status,
      String(parsed.error.code),
      parsed.error.correlation_id,
    );
  }
  if (parsed === null) {
    throw new ApiError(`Malformed response from ${path}`, response.status, 'INTERNAL_ERROR', null);
  }
  return parsed as Envelope<TData>;
}

/** The bearer token for a direct fetch (e.g. an artifact download that is not JSON). */
export function currentAccessToken(): string | null {
  return tokenStore.get()?.accessToken ?? null;
}

/** The raw typed client, exposed for endpoints added in later phases. */
export { raw as apiClient };
