import { useMemo, useState } from 'react';

import { ModuleState } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';
import { ChartWithTable } from './ChartWithTable';
import { formatCents, humanizeEnum } from './format';

/**
 * Render a suppression reason without leaking a raw enum prefix.
 *
 * The backend states a reason as either a bare enum band (`DUPLICATE_PRODUCT`) or a
 * `TOKEN: human detail` string (`COOLING_OFF: declined on 2026-01-15, suppressed until …`). Only
 * the leading uppercase token reads as machine text; humanize just that and keep any detail after
 * the colon verbatim (it carries real dates the reader needs).
 */
function formatSuppression(reason: string | null | undefined): string {
  if (reason === null || reason === undefined || reason === '') {
    return 'Suppressed for this customer.';
  }
  const colon = reason.indexOf(':');
  if (colon === -1) {
    // A bare token like DUPLICATE_PRODUCT, or already-human text.
    return /^[A-Z0-9_]+$/.test(reason) ? humanizeEnum(reason) : reason;
  }
  const token = reason.slice(0, colon);
  const detail = reason.slice(colon + 1).trim();
  const head = /^[A-Z0-9_]+$/.test(token) ? humanizeEnum(token) : token;
  return detail === '' ? head : `${head}: ${detail}`;
}
import { pick, type ContactData, type OfferData, type OffersData, type ProfileData } from './types';
import { deriveWidgetState } from './widgetState';

/** Who to pitch to: the customer's name and (entitlement-permitting) a dialable number / email. */
interface PitchContact {
  readonly name: string;
  readonly email: string | null;
  readonly tel: string | null;
}

/** Extract a display name, a real email, and a dialable number from the loaded 360 payload. */
function pitchContactFrom(raw: Record<string, unknown>): PitchContact {
  const profile = pick<ProfileData>(raw, 'profile');
  const contact = pick<ContactData>(raw, 'contact');
  const name =
    typeof profile?.customer_name === 'string' && profile.customer_name !== ''
      ? profile.customer_name
      : 'this customer';
  // A usable email/number must be real, not a masked partial (contains `*`) or absent.
  const rawEmail = contact?.email;
  const email =
    typeof rawEmail === 'string' && rawEmail.includes('@') && !rawEmail.includes('*')
      ? rawEmail
      : null;
  let tel: string | null = null;
  for (const candidate of [contact?.mobile_number, contact?.phone_number]) {
    if (typeof candidate === 'string' && candidate !== '' && !candidate.includes('*')) {
      const digits = candidate.replace(/[^\d+]/g, '');
      if (digits.replace(/\D/g, '').length >= 7) {
        tel = digits;
        break;
      }
    }
  }
  return { name, email, tel };
}

type OfferFilter = 'all' | 'cross-sell' | 'upsell' | 'active' | 'suppressed';

const FILTER_LABELS: Record<OfferFilter, string> = {
  all: 'All offers',
  'cross-sell': 'Cross-sell',
  upsell: 'Upsell',
  active: 'Active only',
  suppressed: 'Suppressed only',
};

/**
 * Offer intelligence widget (task 13.5, requirements 9.1–9.7).
 *
 * The ranked offer list with every field the backend surfaces: rank, expected value (the ranking
 * basis), the cross-sell / upsell distinction (requirement 9.5), the rationale, and campaign
 * membership. A suppressed offer — a duplicate product, or one inside its declined cooling-off
 * window — is not dropped but de-emphasized (greyed) with its suppression reason visible
 * (requirement 9.6). The propensity is shown as a calibrated bar with a confidence indicator; the
 * confidence tier is carried in the server-computed rationale, so it is read from there rather than
 * re-derived.
 */
export function OffersWidget({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const state = deriveWidgetState(view, { module: 'offers', slots: ['offers'] });
  const offers = pick<OffersData>(view.raw, 'offers');
  const pitch = pitchContactFrom(view.raw);
  const list = useMemo(() => offers?.offers ?? [], [offers]);
  const campaigns = offers?.campaign_ids ?? [];
  const isEmpty = state.isEmpty || list.length === 0;

  const [filter, setFilter] = useState<OfferFilter>('all');

  const filtered = useMemo(() => list.filter((o) => matchesFilter(o, filter)), [list, filter]);
  // Show only the top 3 ranked offer cards to keep the card compact; the full ranked set stays in
  // the accessible data table (toggle) and the count reflects the total.
  const TOP_N = 3;
  const topOffers = filtered.slice(0, TOP_N);
  const maxValue = Math.max(...list.map((o) => Math.max(0, o.expected_value_cents)), 1);

  return (
    <ModuleState title="Offers" state={state.model} isEmpty={isEmpty}>
      {offers !== undefined && list.length > 0 && (
        <div className="widget">
          <div className="offers__toolbar">
            <label className="filter-select">
              <span>Show</span>
              <select
                className="focus-ring"
                value={filter}
                onChange={(event) => {
                  setFilter(event.target.value as OfferFilter);
                }}
              >
                {(Object.keys(FILTER_LABELS) as OfferFilter[]).map((key) => (
                  <option key={key} value={key}>
                    {FILTER_LABELS[key]}
                  </option>
                ))}
              </select>
            </label>
            <span className="offers__count">
              {filtered.length} of {list.length}
            </span>
          </div>

          {filtered.length === 0 ? (
            <p className="module__empty">No offers match this filter.</p>
          ) : (
            <ChartWithTable
              label="Ranked offers with calibrated propensity bars"
              chart={
                <>
                  <ul className="offers">
                    {topOffers.map((offer) => (
                      <OfferCard
                        key={offer.offer_id}
                        offer={offer}
                        maxValue={maxValue}
                        pitch={pitch}
                      />
                    ))}
                  </ul>
                  {filtered.length > TOP_N && (
                    <p className="offers__more">
                      +{filtered.length - TOP_N} more · see the data table for the full ranked list
                    </p>
                  )}
                </>
              }
              table={<OffersTable offers={filtered} />}
            />
          )}
          {campaigns.length > 0 && (
            <p className="offers__campaigns">Campaigns: {campaigns.map((id) => id).join(', ')}</p>
          )}
        </div>
      )}
    </ModuleState>
  );
}

function matchesFilter(offer: OfferData, filter: OfferFilter): boolean {
  switch (filter) {
    case 'all':
      return true;
    case 'cross-sell':
      return offer.is_cross_sell && !offer.is_upsell;
    case 'upsell':
      return offer.is_upsell;
    case 'active':
      return !offer.suppressed;
    case 'suppressed':
      return offer.suppressed;
  }
}

function OfferCard({
  offer,
  maxValue,
  pitch,
}: {
  readonly offer: OfferData;
  readonly maxValue: number;
  readonly pitch: PitchContact;
}): React.JSX.Element {
  const kind = offer.is_upsell ? 'Upsell' : offer.is_cross_sell ? 'Cross-sell' : 'Offer';
  const confidence = confidenceTier(offer.rationale);
  const pct = Math.max(2, (Math.max(0, offer.expected_value_cents) / maxValue) * 100);

  // Pre-composed pitch: a mailto with subject/body naming the offer, and a tel: to call. Both are
  // suppressed for a suppressed offer (nothing to pitch) and disabled when the channel is not
  // available for the role (masked/absent email or number) — no leak, no broken link.
  const subject = `Regarding: ${offer.offer_name}`;
  const bodyLines = [
    `Hi ${pitch.name.split(' ')[0] ?? pitch.name},`,
    '',
    `I'd like to share an offer I think fits you well: ${offer.offer_name}.`,
    '',
    'Would you be open to a quick chat about it?',
  ];
  const mailto =
    pitch.email !== null
      ? `mailto:${encodeURIComponent(pitch.email)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(bodyLines.join('\n'))}`
      : null;

  // A compact card: rank, name, type and expected value are the critical facts; the propensity bar
  // conveys the ranking basis. The wordy rationale and business-group lines are dropped here (they
  // remain in the accessible data table) to keep the card short.
  return (
    <li className={offer.suppressed ? 'offer offer--suppressed' : 'offer'}>
      <div className="offer__head">
        <span className="offer__rank" aria-hidden="true">
          #{offer.rank}
        </span>
        <span className="offer__name">{offer.offer_name}</span>
        <span className={`badge offer__kind offer__kind--${kind.toLowerCase().replace('-', '')}`}>
          {kind}
        </span>
      </div>

      <div className="offer__meta">
        <span className="offer__ev">EV {formatCents(offer.expected_value_cents)}</span>
        {confidence !== null && (
          <span className="offer__confidence" title="Propensity confidence">
            {confidence} confidence
          </span>
        )}
      </div>

      <div
        className="offer__propensity"
        aria-label={`Expected value relative to top offer${confidence !== null ? `, ${confidence} confidence` : ''}`}
      >
        <span className="offer__bar-track">
          <span className="offer__bar-fill" style={{ width: `${pct}%` }} />
        </span>
      </div>

      {offer.suppressed && (
        <p className="offer__suppression">
          <span className="badge badge--restricted">Suppressed</span>{' '}
          {formatSuppression(offer.suppression_reason)}
        </p>
      )}

      {/* Pitch actions (hidden in the compact glance view via CSS). A suppressed offer has nothing
          to pitch. Each channel is a real mailto/tel when available, or a disabled hint otherwise. */}
      {!offer.suppressed && (
        <div className="offer__actions">
          {mailto !== null ? (
            <a className="button button--subtle offer__pitch focus-ring" href={mailto}>
              <span aria-hidden="true">✉️</span> Pitch on email
            </a>
          ) : (
            <span
              className="button button--subtle offer__pitch offer__pitch--disabled"
              aria-disabled="true"
            >
              <span aria-hidden="true">✉️</span> Email unavailable
            </span>
          )}
          {pitch.tel !== null ? (
            <a className="button button--subtle offer__pitch focus-ring" href={`tel:${pitch.tel}`}>
              <span aria-hidden="true">📞</span> Pitch on call
            </a>
          ) : (
            <span
              className="button button--subtle offer__pitch offer__pitch--disabled"
              aria-disabled="true"
            >
              <span aria-hidden="true">📞</span> Call unavailable
            </span>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * The accessible table equivalent of the ranked offer list (task 15.2, requirement 16.2). The card
 * list shows expected value as a propensity bar — a visual encoding; this table carries the same
 * numbers as text, plus the cross-sell/upsell distinction, the confidence tier and the suppression
 * reason, so a screen-reader user gets everything the cards convey.
 */
function OffersTable({ offers }: { readonly offers: readonly OfferData[] }): React.JSX.Element {
  return (
    <table className="data-table">
      <caption>Ranked offers</caption>
      <thead>
        <tr>
          <th scope="col">Rank</th>
          <th scope="col">Offer</th>
          <th scope="col">Type</th>
          <th scope="col">Expected value</th>
          <th scope="col">Confidence</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {offers.map((offer) => {
          const kind = offer.is_upsell ? 'Upsell' : offer.is_cross_sell ? 'Cross-sell' : 'Offer';
          const confidence = confidenceTier(offer.rationale);
          return (
            <tr key={offer.offer_id}>
              <td>{offer.rank}</td>
              <th scope="row">{offer.offer_name}</th>
              <td>{kind}</td>
              <td>{formatCents(offer.expected_value_cents)}</td>
              <td>{confidence ?? '—'}</td>
              <td>
                {offer.suppressed
                  ? `Suppressed: ${formatSuppression(offer.suppression_reason)}`
                  : 'Active'}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * Read the confidence tier out of the server-computed rationale (e.g. "... high confidence").
 *
 * The backend does not return the raw `probability_confidence` separately; it folds it into the
 * value-free rationale string. Rather than invent a number, the widget surfaces the tier the
 * backend already stated, or nothing when the rationale does not carry one.
 */
function confidenceTier(rationale: string): 'low' | 'medium' | 'high' | null {
  const match = /\b(low|medium|high)\s+confidence\b/i.exec(rationale);
  const tier = match?.[1];
  if (tier === undefined) {
    return null;
  }
  return tier.toLowerCase() as 'low' | 'medium' | 'high';
}
