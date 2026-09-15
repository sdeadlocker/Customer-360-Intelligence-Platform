import {
  currentAccessToken,
  deleteEnvelope,
  getEnvelope,
  postJsonEnvelope,
} from '../../api/client';
import type { Envelope } from '../../api/types';

/**
 * The report-exports API (Phase 18, tasks 18.5, 18.6).
 *
 * Thin typed wrappers over the generic envelope helpers. A report is defined for a customer (or a
 * book/segment), run on demand, its run history listed, and its artifact downloaded. The artifact
 * download is a binary (PDF) rather than an envelope, so it goes through a direct authenticated
 * fetch and returns a blob URL the caller revokes when done.
 */

export type ReportType = 'PDF_PACK' | 'MEETING_BRIEFING';
export type ScopeKind = 'CUSTOMER' | 'BOOK' | 'SEGMENT';
export type Cadence = 'DAILY' | 'WEEKLY' | 'MONTHLY';

export interface DefineReportInput {
  readonly report_type: ReportType;
  readonly title: string;
  readonly scope: {
    readonly kind: ScopeKind;
    readonly customer_id?: string;
    readonly customer_ids?: readonly string[];
    readonly segments?: readonly string[];
  };
  readonly branding?: { readonly brand_name?: string; readonly tagline?: string };
}

export interface RunView {
  readonly run_id: number;
  readonly definition_id: number;
  readonly report_type: string;
  readonly status: string;
  readonly trigger_kind: string;
  readonly customers_rendered: number;
  readonly model_ids: readonly string[];
  readonly prompt_versions: readonly string[];
  readonly degraded: boolean;
  readonly started_at: string;
  readonly finished_at: string | null;
  readonly error: string | null;
}

/** Define a report and return its id. */
export async function defineReport(
  input: DefineReportInput,
  signal?: AbortSignal,
): Promise<number> {
  const envelope = await postJsonEnvelope<{ definition_id: number }>('/reports', input, signal);
  return envelope.data.definition_id;
}

/** Run a report now and return the resulting run. */
export async function runReport(definitionId: number, signal?: AbortSignal): Promise<RunView> {
  const envelope: Envelope<RunView> = await postJsonEnvelope<RunView>(
    `/reports/${encodeURIComponent(String(definitionId))}/run`,
    {},
    signal,
  );
  return envelope.data;
}

/** List a definition's run history, newest first. */
export async function listRuns(definitionId: number, signal?: AbortSignal): Promise<RunView[]> {
  const envelope = await getEnvelope<{ runs: RunView[] }>(
    `/reports/${encodeURIComponent(String(definitionId))}/runs`,
    signal,
  );
  return [...(envelope.data.runs ?? [])];
}

/** Attach a schedule to a definition and return its id. */
export async function scheduleReport(
  definitionId: number,
  cadence: Cadence,
  signal?: AbortSignal,
): Promise<number> {
  const envelope = await postJsonEnvelope<{ schedule_id: number }>(
    `/reports/${encodeURIComponent(String(definitionId))}/schedules`,
    { cadence },
    signal,
  );
  return envelope.data.schedule_id;
}

/** Disable a schedule. */
export async function disableSchedule(scheduleId: number, signal?: AbortSignal): Promise<void> {
  await deleteEnvelope<{ disabled: boolean }>(
    `/reports/schedules/${encodeURIComponent(String(scheduleId))}`,
    signal,
  );
}

/**
 * Download a run's artifact as a blob URL. The artifact is a PDF, not an envelope, so this is a
 * direct authenticated fetch; the caller is responsible for revoking the returned object URL.
 */
export async function downloadArtifactUrl(runId: number): Promise<string> {
  const token = currentAccessToken();
  const response = await fetch(`/reports/runs/${encodeURIComponent(String(runId))}`, {
    headers: token !== null ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    throw new Error(`Download failed (${String(response.status)})`);
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}
