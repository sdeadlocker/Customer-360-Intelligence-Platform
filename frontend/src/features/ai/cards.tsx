import { useCitations } from './CitationContext';
import { humanizeRationale, humanizeReason, parseFactLine } from './narrative';
import type {
  AgentCard,
  FinancialHealthOutput,
  JourneyOutput,
  OfferOutput,
  RelationshipOutput,
  RiskOutput,
  SnapshotItem,
  SummaryOutput,
} from './types';

/**
 * The agent-specific card bodies (tasks 14.1, 14.3, 14.4).
 *
 * Each reads its own typed slice of an {@link AgentCard}'s `outputs` and renders the structured
 * fields the design names for that agent (design §8.3), alongside the narrative. The AI chrome —
 * label, timestamp, cache and degraded badges, the citation lists — is supplied by the enclosing
 * {@link ./AiCard.AiCard} shell, so these components render only the agent's own content.
 *
 * A snapshot fact is an *inspectable chip* (task 14.1): clicking it resolves the underlying fact to
 * the deterministic widget that owns it and highlights the field, via the shared citation context.
 */

// ---------------------------------------------------------------- shared bits

/** A snapshot fact rendered as an inspectable chip that opens the owning widget (task 14.1/14.2). */
function SnapshotChip({
  item,
  card,
}: {
  readonly item: SnapshotItem;
  readonly card: AgentCard;
}): React.JSX.Element {
  const { inspectFact } = useCitations();
  const citation = card.fact_citations.find((c) => c.fact_id === item.fact_id);
  // A snapshot label is either a raw field token (`customer_name`) or an already-human phrase
  // (`Net worth $120K`). Humanize only the machine form; leave a human label untouched.
  const label = isFieldToken(item.label) ? humanize(item.label) : item.label;
  if (citation === undefined) {
    return <span className="snapshot-chip">{label}</span>;
  }
  return (
    <button
      type="button"
      className="snapshot-chip snapshot-chip--linked"
      onClick={() => {
        inspectFact(citation);
      }}
      title={`Open ${citation.entity_type} · ${citation.field}`}
    >
      {label}
    </button>
  );
}

/**
 * Render an agent narrative readably.
 *
 * A degraded / template narrative arrives as a single run-on string of "- field is value [F1]"
 * fragments with no line breaks, which renders as one dense block. We split it into a lead-in
 * sentence plus one line per "- " fragment so the card reads as a list rather than a wall of text,
 * and collapse anything past the first few lines behind a "See more" toggle.
 */
function Narrative({ text }: { readonly text: string }): React.JSX.Element | null {
  const trimmed = text.trim();

  if (trimmed === '') {
    return null;
  }

  const parts = splitNarrative(trimmed);

  // Plain prose (no bullet fragments): render as a paragraph.
  if (parts.bullets.length === 0) {
    return <p className="ai-card__narrative">{parts.lead}</p>;
  }

  // Show the complete narrative in the full views (Profile / 360° Cockpit) — every fact line is
  // rendered. In the compact glance view the card's height cap clips visually, but no information is
  // removed from the model here.
  return (
    <div className="ai-card__narrative">
      {parts.lead !== '' && <p className="ai-narrative__lead">{parts.lead}</p>}
      <ul className="ai-narrative__points">
        {parts.bullets.map((point, i) => (
          <NarrativePoint key={i} text={point} />
        ))}
      </ul>
    </div>
  );
}

/**
 * One narrative bullet. A template (degraded) narrative's bullet is a machine fact line —
 * `entity.field is value [F1]` — which we render as a clean label / value pair with the citation as
 * a subtle marker. Anything that does not parse as a fact line is genuine prose and renders as-is.
 */
function NarrativePoint({ text }: { readonly text: string }): React.JSX.Element {
  const fact = parseFactLine(text);
  if (fact === null) {
    return <li>{text}</li>;
  }
  return (
    <li className="ai-narrative__fact">
      <span className="ai-narrative__fact-label">{fact.label}</span>
      <span className="ai-narrative__fact-value">{fact.value}</span>
      {fact.citation !== null && (
        <span className="ai-narrative__fact-cite" aria-hidden="true">
          {fact.citation}
        </span>
      )}
    </li>
  );
}

/** Split a narrative into an optional lead sentence and its "- " bullet fragments. */
function splitNarrative(text: string): { lead: string; bullets: string[] } {
  const firstBullet = text.indexOf('- ');
  if (firstBullet === -1) {
    return { lead: text, bullets: [] };
  }
  const lead = text
    .slice(0, firstBullet)
    .replace(/[:\s]+$/, '')
    .trim();
  const bullets = text
    .slice(firstBullet)
    .split(/\s-\s|^-\s/)
    .map((s) => s.replace(/^-\s*/, '').trim())
    .filter((s) => s !== '');
  return { lead, bullets };
}

// ---------------------------------------------------------------- 14.1 summary

/** The executive-summary card body: summary prose, snapshot chips and advisor notes. */
export function SummaryCardBody({ card }: { readonly card: AgentCard }): React.JSX.Element {
  const out = card.outputs as unknown as SummaryOutput;
  const summary = out.executive_summary !== '' ? out.executive_summary : out.narrative;
  return (
    <div className="ai-summary">
      <Narrative text={summary} />

      {out.snapshot.length > 0 && (
        <div className="ai-summary__snapshot">
          <h3 className="widget__subhead">Snapshot</h3>
          <ul className="snapshot-chips">
            {out.snapshot.map((item) => (
              <li key={item.fact_id}>
                <SnapshotChip item={item} card={card} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {out.advisor_notes.length > 0 && (
        <div className="ai-summary__notes">
          <h3 className="widget__subhead">Advisor notes</h3>
          <ul className="advisor-notes">
            {out.advisor_notes.map((note, i) => (
              <li key={i} className="advisor-note">
                {note.note}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 14.3 next best actions

/** The offer/next-best-action card body: ranked offers with rationale and cited eligibility. */
export function OfferCardBody({ card }: { readonly card: AgentCard }): React.JSX.Element {
  const out = card.outputs as unknown as OfferOutput;
  return (
    <div className="ai-offers">
      <Narrative text={out.narrative} />

      {out.ranked_offers.length > 0 ? (
        <ol className="nba-list">
          {out.ranked_offers.map((offer) => (
            <li key={offer.offer_id} className={`nba${offer.suppressed ? ' nba--suppressed' : ''}`}>
              <div className="nba__head">
                <span className="nba__id mono">{offer.offer_id}</span>
                {offer.suppressed && (
                  <span className="badge badge--suppressed" title={offer.suppression_reason ?? ''}>
                    Suppressed
                  </span>
                )}
              </div>
              <p className="nba__rationale">{humanizeRationale(offer.rationale)}</p>
              {offer.suppressed && offer.suppression_reason != null && (
                <p className="nba__suppression">
                  Reason: {humanizeReason(offer.suppression_reason)}
                </p>
              )}
            </li>
          ))}
        </ol>
      ) : (
        <p className="module__empty">No recommendations for this customer.</p>
      )}

      {out.eligibility_notes.length > 0 && (
        <div className="nba__eligibility">
          <h3 className="widget__subhead">Eligibility (from product knowledge)</h3>
          <ul>
            {out.eligibility_notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 14.4 narrative cards

/** Financial health narrative card: score/band, drivers and snapshot chips. */
export function FinancialHealthCardBody({ card }: { readonly card: AgentCard }): React.JSX.Element {
  const out = card.outputs as unknown as FinancialHealthOutput;
  return (
    <div className="ai-narrative-card">
      <Narrative text={out.narrative} />
      {out.health_band != null && (
        <p className="ai-headline">
          Financial health:{' '}
          <strong>
            {isFieldToken(out.health_band) ? humanize(out.health_band) : out.health_band}
          </strong>
          {out.health_score != null && (
            <span className="ai-headline__score"> ({out.health_score})</span>
          )}
        </p>
      )}
      {out.drivers.length > 0 && (
        <ul className="driver-notes">
          {out.drivers.map((driver) => (
            <li key={driver.factor}>
              <strong>{driver.factor}</strong> ({driver.contribution >= 0 ? '+' : ''}
              {driver.contribution}): {driver.detail}
            </li>
          ))}
        </ul>
      )}
      {out.snapshot.length > 0 && (
        <ul className="snapshot-chips">
          {out.snapshot.map((item) => (
            <li key={item.fact_id}>
              <SnapshotChip item={item} card={card} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Risk narrative card: band, drivers, alerts, compliance flag and cited next steps. */
export function RiskCardBody({ card }: { readonly card: AgentCard }): React.JSX.Element {
  const out = card.outputs as unknown as RiskOutput;
  return (
    <div className="ai-narrative-card">
      {out.compliance_flag && (
        <div className="banner banner--compliance" role="alert">
          <strong>Compliance review required.</strong> This customer carries an AML or PEP
          indicator.
        </div>
      )}
      <Narrative text={out.narrative} />
      {out.risk_band != null && (
        <p className="ai-headline">
          Risk band:{' '}
          <strong>{isFieldToken(out.risk_band) ? humanize(out.risk_band) : out.risk_band}</strong>
        </p>
      )}
      {out.drivers.length > 0 && (
        <ul className="driver-notes">
          {out.drivers.map((driver) => (
            <li key={driver.factor}>
              <strong>{driver.factor}</strong> — {driver.severity}
            </li>
          ))}
        </ul>
      )}
      {out.alerts.length > 0 && (
        <ul className="risk-alerts">
          {out.alerts.map((alert, i) => (
            <li key={i}>{alert}</li>
          ))}
        </ul>
      )}
      {out.next_steps.length > 0 && (
        <div className="risk-next-steps">
          <h3 className="widget__subhead">Prescribed next steps (from procedure knowledge)</h3>
          <ol>
            {out.next_steps.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

/** Relationship narrative card: household context and network insights. */
export function RelationshipCardBody({ card }: { readonly card: AgentCard }): React.JSX.Element {
  const out = card.outputs as unknown as RelationshipOutput;
  return (
    <div className="ai-narrative-card">
      <Narrative text={out.narrative} />
      {(out.household_id != null || out.member_count != null) && (
        <p className="ai-headline">
          {out.household_id != null && <>Household {out.household_id}</>}
          {out.member_count != null && <> · {out.member_count} members</>}
        </p>
      )}
      {out.insights.length > 0 && (
        <ul className="driver-notes">
          {out.insights.map((insight, i) => (
            <li key={i}>{insight}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Journey narrative card: the growth story and its milestones. */
export function JourneyCardBody({ card }: { readonly card: AgentCard }): React.JSX.Element {
  const out = card.outputs as unknown as JourneyOutput;
  return (
    <div className="ai-narrative-card">
      <Narrative text={out.narrative} />
      {out.milestones.length > 0 && (
        <ul className="milestone-notes">
          {out.milestones.map((milestone, i) => (
            <li key={i}>
              <strong>{humanize(milestone.milestone_type)}</strong>: {milestone.detail}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function humanize(value: string): string {
  const lowered = value.replace(/_/g, ' ').toLowerCase();
  return lowered.charAt(0).toUpperCase() + lowered.slice(1);
}

/**
 * A raw field/enum token — `customer_name`, `NET_WORTH`, `SILVER` — as opposed to an already-human
 * phrase like `Net worth $120K`. Only such tokens should be humanized; a phrase with spaces or
 * mixed case is display text and must pass through unchanged.
 */
function isFieldToken(value: string): boolean {
  return /^[a-z][a-z0-9_]*$/.test(value) || /^[A-Z0-9_]+$/.test(value);
}
