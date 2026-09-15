import { getEnvelope } from '../../api/client';
import type { Customer360, ModuleError } from '../../api/types';

/**
 * Fetch and normalise the composed 360 read model (design §6.3, `GET /customers/{id}/360`).
 *
 * The endpoint returns a partial-tolerant payload: present modules appear as top-level keys, failed
 * modules are absent and recorded in `meta.errors[]`, and any redaction is listed in
 * `meta.masked_fields` with a module prefix. The shell needs three derived things per module —
 * whether it is present, whether it failed, and which of its fields were masked — so this normalises
 * the raw envelope into exactly that, keeping the page free of envelope-shape knowledge.
 *
 * The payload's `data` is `dict[str, Any]` on the wire (the composed view is dynamic), so it is read
 * defensively here rather than trusted to a rigid type; the individual module shapes are refined in
 * Phase 13 when each widget consumes its own field set.
 */

export interface Dashboard360 {
  readonly customerId: string;
  /** Top-level module keys the payload actually carried (present ⇒ not empty, not failed). */
  readonly present: ReadonlySet<string>;
  /** Prefixed masked-field paths from `meta.masked_fields`, e.g. `profile.dob`. */
  readonly maskedFields: readonly string[];
  /** Per-module failure notices from `meta.errors[]`. */
  readonly errors: readonly ModuleError[];
  /** The raw payload, for Phase 13 widgets to read their own slices from. */
  readonly raw: Customer360;
}

const NON_MODULE_KEYS = new Set(['customer_id']);

export async function request360(customerId: string, signal?: AbortSignal): Promise<Dashboard360> {
  const path = `/customers/${encodeURIComponent(customerId)}/360`;
  const envelope = await getEnvelope<Customer360>(path, signal);
  const data = envelope.data;
  const present = new Set(Object.keys(data).filter((key) => !NON_MODULE_KEYS.has(key)));

  return {
    customerId,
    present,
    maskedFields: envelope.meta.masked_fields ?? [],
    errors: envelope.meta.errors ?? [],
    raw: data,
  };
}
