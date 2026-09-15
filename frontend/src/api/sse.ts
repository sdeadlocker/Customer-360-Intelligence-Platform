/**
 * A minimal Server-Sent-Events reader for the AI streaming endpoints (Phase 14).
 *
 * The backend hand-rolls SSE frames — an `event:` line, a `data:` JSON line and a terminating blank
 * line (see `c360/agents/streaming.py` and `c360/agents/qa_streaming.py`). The browser's native
 * `EventSource` only issues GET requests and cannot attach an `Authorization` header, but every AI
 * endpoint is bearer-authenticated and `/ask` is a POST with a JSON body. So the stream is read from
 * `fetch` instead, and this module parses the frame protocol off the response body.
 *
 * Two rules shape it:
 *
 * - **Per-event delivery.** Each complete frame is dispatched as soon as its blank-line terminator
 *   arrives, so a fast agent card renders without waiting on a slow one — the whole point of
 *   streaming the dashboard (design §8.1). Frames are buffered across chunk boundaries because a
 *   read can split a frame anywhere.
 * - **Abortable.** The reader honours an `AbortSignal`, so a component that unmounts (a customer
 *   switch) cancels the stream rather than leaking a dangling fetch — the same discipline the REST
 *   client uses.
 */

import { tokenStore } from '../auth/tokenStore';

/** One parsed SSE frame: the `event:` name and the decoded `data:` JSON payload. */
export interface SseEvent<TData = unknown> {
  readonly event: string;
  readonly data: TData;
}

export interface SseRequest {
  readonly path: string;
  /** HTTP method; SSE endpoints are GET (insights/recommendations) or POST (ask). */
  readonly method?: 'GET' | 'POST';
  /** JSON body for a POST stream (the ask question + session). */
  readonly body?: unknown;
  readonly signal?: AbortSignal;
}

/** Thrown when the stream could not be opened (a non-2xx status before any frame). */
export class SseError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'SseError';
    this.status = status;
  }
}

/**
 * Open an authenticated SSE stream and yield each frame as it completes.
 *
 * The bearer token is attached from the same store the REST client uses. A non-OK response before
 * any frame is an `SseError` (a 401/403/404/503 the caller renders as a failed card); once the
 * stream is open, transport errors surface as the async iterator ending or throwing, which the
 * consuming hook treats as a stream failure.
 */
export async function* openSse(request: SseRequest): AsyncGenerator<SseEvent, void, void> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  const tokens = tokenStore.get();
  if (tokens !== null) {
    headers.Authorization = `Bearer ${tokens.accessToken}`;
  }
  const init: RequestInit = {
    method: request.method ?? 'GET',
    headers,
    ...(request.signal ? { signal: request.signal } : {}),
  };
  if (request.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(request.body);
  }

  const response = await fetch(request.path, init);
  if (!response.ok || response.body === null) {
    throw new SseError(`stream failed for ${request.path}`, response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      // A frame is terminated by a blank line. Split on the blank-line boundary and keep the last,
      // possibly-incomplete, segment in the buffer until its terminator arrives.
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const raw = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const frame = parseFrame(raw);
        if (frame !== null) {
          yield frame;
        }
        boundary = buffer.indexOf('\n\n');
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * Parse one raw frame (`event: x\ndata: {...}`) into an {@link SseEvent}. A frame with no `data:`
 * line, or a malformed JSON payload, is dropped (returns `null`) rather than crashing the stream —
 * a robustness the hand-rolled protocol warrants.
 */
export function parseFrame(raw: string): SseEvent | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart());
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) as unknown };
  } catch {
    return null;
  }
}
