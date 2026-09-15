import { describe, expect, it } from 'vitest';

import { parseFrame } from './sse';

/**
 * The SSE frame parser underpins every AI stream (Phase 14). These assert it decodes the backend's
 * hand-rolled `event:`/`data:` framing and drops malformed frames rather than crashing the stream.
 */
describe('parseFrame', () => {
  it('parses an event name and a JSON data payload', () => {
    const frame = parseFrame('event: agent\ndata: {"agent":"risk","degraded":false}');
    expect(frame).not.toBeNull();
    expect(frame?.event).toBe('agent');
    expect(frame?.data).toEqual({ agent: 'risk', degraded: false });
  });

  it('defaults the event name to "message" when only data is present', () => {
    const frame = parseFrame('data: {"text":"hi"}');
    expect(frame?.event).toBe('message');
    expect(frame?.data).toEqual({ text: 'hi' });
  });

  it('returns null for a frame with no data line', () => {
    expect(parseFrame('event: done')).toBeNull();
  });

  it('returns null for malformed JSON rather than throwing', () => {
    expect(parseFrame('event: agent\ndata: {not json}')).toBeNull();
  });
});
