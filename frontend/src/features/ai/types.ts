/**
 * Client-facing shapes for the AI experience layer (Phase 14).
 *
 * These mirror the SSE payloads the backend emits — one `agent` frame per dashboard card
 * (`c360/agents/streaming.py`) and the `token` / `citations` / `done` frames of the Q&A stream
 * (`c360/agents/qa_streaming.py`). They are hand-declared rather than generated because the SSE
 * bodies are not part of the OpenAPI schema (the streaming responses are typed only as
 * `text/event-stream`), so this module is the single place the frame contract is written down.
 *
 * Two kinds of citation exist and are kept apart everywhere, exactly as the backend does (design
 * §8.4/§9.1): a {@link FactCitation} resolves to a field the deterministic widgets already show; a
 * {@link PassageCitation} resolves to a knowledge passage. A figure may cite a fact; guidance may
 * cite a passage; a figure may never be sourced from a passage alone.
 */

/** A citation to a fact the dashboard's deterministic widgets already render. */
export interface FactCitation {
  readonly kind: 'fact';
  readonly fact_id: string;
  readonly entity_type: string;
  readonly entity_id: string;
  readonly field: string;
}

/** A citation to a retrieved knowledge passage (document, section, version, effective window). */
export interface PassageCitation {
  readonly kind: 'passage';
  readonly passage_id: string;
  readonly doc_id: string;
  readonly section_path: string;
  readonly title: string;
  readonly version: string;
  readonly effective_from: string;
  readonly effective_to?: string | null;
}

export type Citation = FactCitation | PassageCitation;

// ---------------------------------------------------------------- agent card (insights / recs)

/** The known agent node names the dashboard graph emits, in wave order (design §8.1). */
export type AgentName =
  | 'financial_health'
  | 'risk'
  | 'life_event'
  | 'relationship'
  | 'offer_recommendation'
  | 'journey'
  | 'customer_summary';

/**
 * One agent card as delivered on an SSE `agent` frame. `outputs` is the agent's typed payload
 * (schema per agent, see `c360/agents/schemas.py`); it is read structurally by each card component,
 * so it is left as an open record here rather than a union that would couple every card to every
 * schema. `agent` is a plain string on the wire — the {@link AgentName} names the ones the UI keys
 * cards off, but an unrecognised agent must not break parsing.
 */
export interface AgentCard {
  readonly agent: string;
  readonly outputs: Readonly<Record<string, unknown>>;
  readonly fact_citations: readonly FactCitation[];
  readonly passage_citations: readonly PassageCitation[];
  readonly unavailable_inputs: readonly string[];
  readonly confidence: number | null;
  readonly generated_at: string;
  readonly model_id: string;
  readonly prompt_version: string;
  readonly cache_hit: boolean;
  readonly degraded: boolean;
}

// ---------------------------------------------------------------- typed agent outputs

export interface SnapshotItem {
  readonly label: string;
  readonly fact_id: string;
}

export interface AdvisorNote {
  readonly note: string;
}

export interface SummaryOutput {
  readonly narrative: string;
  readonly executive_summary: string;
  readonly snapshot: readonly SnapshotItem[];
  readonly advisor_notes: readonly AdvisorNote[];
}

export interface HealthDriver {
  readonly factor: string;
  readonly contribution: number;
  readonly detail: string;
}

export interface FinancialHealthOutput {
  readonly narrative: string;
  readonly health_score: number | null;
  readonly health_band: string | null;
  readonly drivers: readonly HealthDriver[];
  readonly snapshot: readonly SnapshotItem[];
}

export interface RiskDriver {
  readonly factor: string;
  readonly severity: string;
}

export interface RiskOutput {
  readonly narrative: string;
  readonly risk_band: string | null;
  readonly drivers: readonly RiskDriver[];
  readonly alerts: readonly string[];
  readonly compliance_flag: boolean;
  readonly next_steps: readonly string[];
}

export interface LifeEventItem {
  readonly event_type: string;
  readonly confidence: number | null;
  readonly inferred: boolean;
}

export interface RelationshipOutput {
  readonly narrative: string;
  readonly household_id: string | null;
  readonly member_count: number | null;
  readonly insights: readonly string[];
}

export interface RankedOffer {
  readonly offer_id: string;
  readonly rationale: string;
  readonly suppressed: boolean;
  readonly suppression_reason: string | null;
}

export interface OfferOutput {
  readonly narrative: string;
  readonly ranked_offers: readonly RankedOffer[];
  readonly eligibility_notes: readonly string[];
}

export interface JourneyMilestone {
  readonly milestone_type: string;
  readonly detail: string;
}

export interface JourneyOutput {
  readonly narrative: string;
  readonly milestones: readonly JourneyMilestone[];
}

// ---------------------------------------------------------------- Q&A stream

/** The `citations` frame of the Q&A stream: facts and knowledge kept apart, plus traversal paths. */
export interface QaCitations {
  readonly fact_citations: readonly FactCitation[];
  readonly passage_citations: readonly PassageCitation[];
  /** Graph traversal path(s) used to answer a relationship question (task 9.4). */
  readonly traversal_paths: readonly (readonly string[])[];
}

/** The terminal `done` frame of the Q&A stream: behavioural flags and labelling metadata. */
export interface QaDone {
  readonly refused: boolean;
  readonly no_guidance: boolean;
  readonly degraded: boolean;
  readonly route: string;
  readonly model_id: string;
}
