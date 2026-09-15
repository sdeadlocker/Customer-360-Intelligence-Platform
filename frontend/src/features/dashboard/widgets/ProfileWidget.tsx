import { ModuleState } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';
import {
  formatDate,
  formatNumber,
  humanizeEnum,
  humanizeMasked,
  provenance,
  type Maskable,
} from './format';
import { pick, type ProfileData } from './types';
import { deriveWidgetState } from './widgetState';

/**
 * Profile and demographics widget (task 13.1, requirements 4.5, 4.6, 4.8).
 *
 * Shows every profile field the design lists, each masked exactly as the server already delivered
 * it (a name arrives as `"Renata ***"`, a date of birth as a year, a value tier possibly hidden),
 * with the profile row's own as-of timestamp for provenance (requirement 4.8). The widget does no
 * masking itself: it renders what the field-masking serializer produced and lets the `restricted`
 * badge (driven by `meta.masked_fields`) explain the gaps.
 */
export function ProfileWidget({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const state = deriveWidgetState(view, { module: 'profile', slots: ['profile'] });
  const profile = pick<ProfileData>(view.raw, 'profile');

  const name = maskedText(profile?.customer_name);
  const tier = humanizeMasked(profile?.customer_value);
  const segment = humanizeEnum(profile?.customer_segment);

  return (
    <ModuleState title="Profile" state={state.model} isEmpty={state.isEmpty}>
      {profile !== undefined && (
        <div className="widget">
          {/* An identity header: a gradient avatar with the customer's initials, their name, and
              the segment / value-tier as colourful chips — a face for the record, not just a list. */}
          <header className="profile-hero">
            <span
              className={`profile-hero__avatar profile-hero__avatar--${segmentTone(profile.customer_segment)}`}
              aria-hidden="true"
            >
              {initials(name, profile.customer_id)}
            </span>
            <div className="profile-hero__body">
              <p className="profile-hero__name">{name}</p>
              <p className="profile-hero__id mono">{profile.customer_id}</p>
              <div className="profile-hero__chips">
                {segment !== '—' && (
                  <span
                    className={`badge profile-chip profile-chip--${segmentTone(profile.customer_segment)}`}
                  >
                    {segment}
                  </span>
                )}
                {tier !== '—' && (
                  <span className="badge profile-chip profile-chip--tier">{tier}</span>
                )}
              </div>
            </div>
          </header>

          <dl className="kv kv--iconed">
            <Row icon="🪪" label="Type" value={humanizeEnum(profile.customer_type)} />
            <Row icon="📅" label="Customer since" value={formatDate(profile.customer_since)} />
            <Row icon="🎂" label="Date of birth" value={maskedText(profile.date_of_birth)} />
            <Row icon="🌐" label="Citizenship" value={profile.citizenship ?? '—'} />
            <Row icon="💍" label="Marital status" value={humanizeEnum(profile.marital_status)} />
            <Row icon="💼" label="Occupation" value={profile.occupation ?? '—'} />
            <Row icon="🏢" label="Employment" value={humanizeEnum(profile.employment_status)} />
            <Row icon="🆔" label="Employer" value={profile.employer_id ?? '—'} />
            <Row icon="🗣️" label="Preferred language" value={profile.preferred_language ?? '—'} />
            <Row
              icon="📨"
              label="Preferred channel"
              value={humanizeEnum(profile.preferred_channel)}
            />
            <Row
              icon="⭐"
              label="Value score"
              value={
                profile.customer_value_score != null
                  ? formatNumber(Math.round(profile.customer_value_score))
                  : '—'
              }
            />
          </dl>
          <p className="widget__provenance">
            {provenance(profile.as_of_date, profile.source_system)}
          </p>
        </div>
      )}
    </ModuleState>
  );
}

/** A tone keyword per segment, used to colour the avatar and segment chip. */
function segmentTone(segment: string | null | undefined): string {
  switch (segment) {
    case 'UHNW':
    case 'HNW':
      return 'gold';
    case 'AFFLUENT':
      return 'teal';
    case 'SMALL_BUSINESS':
      return 'violet';
    default:
      return 'indigo';
  }
}

/** Up to two initials from a (possibly masked) name, falling back to the customer id. */
function initials(name: string, customerId: string): string {
  const words = name
    .replace(/[^a-zA-Z\s]/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) {
    return (
      customerId
        .replace(/[^a-zA-Z0-9]/g, '')
        .slice(-2)
        .toUpperCase() || '👤'
    );
  }
  const first = words[0]?.[0] ?? '';
  const second = words.length > 1 ? (words[words.length - 1]?.[0] ?? '') : '';
  return (first + second).toUpperCase();
}

/** Render a maskable value: a masked field is a string, a raw one may be a number. */
function maskedText(value: Maskable): string {
  if (value === null || value === undefined || value === '') {
    return '—';
  }
  return typeof value === 'number' ? String(value) : value;
}

function Row({
  label,
  value,
  icon,
}: {
  readonly label: string;
  readonly value: string;
  readonly icon?: string;
}): React.JSX.Element {
  return (
    <>
      <dt className="kv__key" data-icon={icon}>
        {label}
      </dt>
      <dd className="kv__val">{value}</dd>
    </>
  );
}
