/**
 * Convenience aliases over the generated OpenAPI schema (task 12.1).
 *
 * `src/api/generated/schema.ts` is machine-generated from `openapi.json`; the drift check makes a
 * stale copy a build failure. This module gives the rest of the app short, stable names for the
 * schema shapes it uses, so a component imports `Customer360` rather than
 * `components['schemas']['Envelope_dict_str__Any__']`. The generated file is the source of truth —
 * every alias here resolves into it, so a backend contract change flows through automatically once
 * the spec is re-exported.
 *
 * Monetary values arrive from the backend as integer cents (`*_cents`) or decimal strings and are
 * never re-typed as `number` for display maths — the backend keeps exact cents and only widens at
 * the boundary, and a float here would undo that.
 */

import type { components } from './generated/schema';

type Schemas = components['schemas'];

/** The response envelope (design §6.2). Generic over the data payload. */
export interface Envelope<TData> {
  readonly data: TData;
  readonly meta: Meta;
}

export type Meta = Schemas['Meta'];
export type ModuleError = Schemas['ModuleError'];
export type ErrorCode = Schemas['ErrorCode'];

export type HealthPayload = Schemas['HealthPayload'];
export type ReadyPayload = Schemas['ReadyPayload'];
export type ComponentPayload = Schemas['ComponentPayload'];
export type ComponentStatus = Schemas['CheckStatus'];

export type TokenPayload = Schemas['TokenPayload'];
export type MeResponse = Schemas['MeResponse'];
export type EntitlementSummary = Schemas['EntitlementSummary'];

export type CustomerSearchHit = Schemas['CustomerSearchHit'];
export type CustomerSegment = Schemas['CustomerSegment'];
export interface Page<TItem> {
  readonly items?: readonly TItem[];
  readonly next_cursor?: string | null;
}

/** The composed 360 read model is `dict[str, Any]` on the wire; a UI-facing shape is refined later. */
export type Customer360 = Schemas['Envelope_dict_str__Any__']['data'];

export interface ErrorBody {
  readonly code: ErrorCode;
  readonly message: string;
  readonly correlation_id: string;
}

export interface ErrorEnvelope {
  readonly error: ErrorBody;
}

/** The six seeded roles the local provider issues tokens for (design §7.1). */
export type Role = 'RM' | 'WEALTH_ADVISOR' | 'CONTACT_CENTER' | 'BRANCH' | 'RISK' | 'MARKETING';
