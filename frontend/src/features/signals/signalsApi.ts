import { getEnvelope, postEnvelope } from '../../api/client';
import type { Envelope, Page } from '../../api/types';

/**
 * The signals worklist API (Phase 17, tasks 17.5, 17.6).
 *
 * Thin typed wrappers over the generic envelope helpers. The signal shape is refined here rather
 * than pulled from the generated schema because the worklist payload is a masked `dict` on the wire
 * (its `value_at_stake_cents` is banded per role by the field-masking serializer), so the value
 * field arrives as a number *or* a band string and the UI must render either without doing maths on
 * it — the same integer-cents discipline the rest of the client keeps.
 */

export type SignalType = 'RISK_BAND_UP' | 'AML_PEP_FLAG' | 'LARGE_DEPOSIT' | 'LIFE_EVENT';
export type Severity = 'INFO' | 'WARNING' | 'CRITICAL';
export type SignalStatus = 'NEW' | 'SEEN' | 'DISMISSED' | 'ACTIONED';

export interface SignalCitation {
  readonly entity_type: string;
  readonly entity_id: string;
  readonly field: string;
  readonly as_of: string;
}

export interface SignalEvidence {
  readonly summary: string;
  readonly citations?: readonly SignalCitation[];
  readonly details?: Readonly<Record<string, string>>;
}

export interface Signal {
  readonly signal_id: number;
  readonly customer_id: string;
  readonly signal_type: SignalType;
  readonly severity: Severity;
  readonly score: number;
  /** Integer cents when the role may see balances, otherwise a banded label string. May be absent
   * if a role has the value hidden. Never used in arithmetic. */
  readonly value_at_stake_cents?: number | string;
  readonly evidence: SignalEvidence;
  readonly as_of: string;
  readonly detected_at: string;
  readonly status: SignalStatus;
}

export interface SignalFilters {
  readonly types?: readonly SignalType[];
  readonly minSeverity?: Severity;
}

/** One page of the ranked worklist. */
export interface WorklistPage {
  readonly items: readonly Signal[];
  readonly nextCursor: string | null;
}

function buildQuery(
  limit: number,
  cursor: string | null | undefined,
  filters: SignalFilters | undefined,
): string {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor != null && cursor !== '') {
    query.set('cursor', cursor);
  }
  if (filters?.types && filters.types.length > 0) {
    query.set('type', filters.types.join(','));
  }
  if (filters?.minSeverity) {
    query.set('severity', filters.minSeverity);
  }
  return query.toString();
}

/** Fetch the caller's ranked, entitlement-scoped worklist (cross-book). */
export async function fetchWorklist(
  {
    limit = 25,
    cursor = null,
    filters,
  }: { limit?: number; cursor?: string | null; filters?: SignalFilters } = {},
  signal?: AbortSignal,
): Promise<WorklistPage> {
  const envelope: Envelope<Page<Signal>> = await getEnvelope<Page<Signal>>(
    `/signals?${buildQuery(limit, cursor, filters)}`,
    signal,
  );
  return {
    items: envelope.data.items ?? [],
    nextCursor: envelope.data.next_cursor ?? null,
  };
}

/** Dismiss a signal for the calling user (suppressed for the cooling-off window). */
export async function dismissSignal(signalId: number, signal?: AbortSignal): Promise<void> {
  await postEnvelope<{ signal_id: number; status: string }>(
    `/signals/${encodeURIComponent(String(signalId))}/dismiss`,
    signal,
  );
}

/** Acknowledge (mark actioned) a signal for the calling user. */
export async function acknowledgeSignal(signalId: number, signal?: AbortSignal): Promise<void> {
  await postEnvelope<{ signal_id: number; status: string }>(
    `/signals/${encodeURIComponent(String(signalId))}/ack`,
    signal,
  );
}
