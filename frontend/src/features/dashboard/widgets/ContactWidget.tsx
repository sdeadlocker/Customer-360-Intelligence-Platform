import { ModuleState } from '../../../components/ModuleState';
import type { Dashboard360 } from '../dashboardData';
import { provenance, type Maskable } from './format';
import { pick, type ContactData } from './types';
import { deriveWidgetState } from './widgetState';

/**
 * Contact widget (task 13.1, requirements 4.5, 4.6, 4.8).
 *
 * Every field here is masked for at least one role (design §7.2): email, phone and mobile are
 * hidden or partialled, street address is redacted, only city/state/country survive for the
 * least-privileged roles. The widget renders whatever the serializer produced — a partial string,
 * or the field simply missing — and the `restricted` badge accounts for it.
 */
export function ContactWidget({ view }: { readonly view: Dashboard360 }): React.JSX.Element {
  const state = deriveWidgetState(view, { module: 'contact', slots: ['contact'] });
  const contact = pick<ContactData>(view.raw, 'contact');

  const dialable = contact !== undefined ? dialableNumber(contact) : null;

  return (
    <ModuleState title="Contact" state={state.model} isEmpty={state.isEmpty}>
      {contact !== undefined && (
        <div className="widget">
          {/* A prominent call action. When a real, dialable number is available it is a tel: link
              (a device/softphone can place the call); when the number is masked or absent for the
              role it stays visible but disabled, so the affordance is discoverable without leaking
              a number the role cannot see. */}
          <CallAction dialable={dialable} />

          <dl className="kv kv--iconed">
            <Row icon="✉️" label="Email" value={text(contact.email)} />
            <Row icon="📞" label="Phone" value={text(contact.phone_number)} />
            <Row icon="📱" label="Mobile" value={text(contact.mobile_number)} />
            <Row icon="🏠" label="Address" value={address(contact)} />
            <Row icon="🏙️" label="City" value={contact.city ?? '—'} />
            <Row icon="🗺️" label="State" value={contact.state ?? '—'} />
            <Row icon="📮" label="Postal code" value={text(contact.postal_code)} />
            <Row icon="🌍" label="Country" value={contact.country ?? '—'} />
          </dl>
          <p className="widget__provenance">
            {provenance(contact.as_of_date, contact.source_system)}
          </p>
        </div>
      )}
    </ModuleState>
  );
}

/**
 * The best number to call: mobile preferred, then phone — but only if it is a real, dialable value
 * (at least 7 digits and no masking marker like `*`). A masked partial (`+1-555-***-0178`) or a
 * fully hidden field returns null, so the call button shows but stays disabled rather than dialing a
 * broken number or implying access the role does not have.
 */
function dialableNumber(contact: ContactData): { label: string; tel: string } | null {
  for (const raw of [contact.mobile_number, contact.phone_number]) {
    if (typeof raw !== 'string' || raw === '' || raw.includes('*')) {
      continue;
    }
    const digits = raw.replace(/[^\d+]/g, '');
    if (digits.replace(/\D/g, '').length >= 7) {
      return { label: raw, tel: digits };
    }
  }
  return null;
}

function CallAction({
  dialable,
}: {
  readonly dialable: { label: string; tel: string } | null;
}): React.JSX.Element {
  if (dialable === null) {
    return (
      <div className="contact-call contact-call--disabled" aria-hidden="true">
        <span className="contact-call__icon">📞</span>
        <span className="contact-call__text">
          <span className="contact-call__title">Call customer</span>
          <span className="contact-call__sub">No dialable number for your role</span>
        </span>
      </div>
    );
  }
  return (
    <a className="contact-call focus-ring" href={`tel:${dialable.tel}`}>
      <span className="contact-call__icon" aria-hidden="true">
        📞
      </span>
      <span className="contact-call__text">
        <span className="contact-call__title">Call customer</span>
        <span className="contact-call__sub mono">{dialable.label}</span>
      </span>
      <span className="contact-call__cta" aria-hidden="true">
        Call
      </span>
    </a>
  );
}

function address(contact: ContactData): string {
  const parts = [contact.address_line1, contact.address_line2]
    .map(text)
    .filter((line) => line !== '—');
  return parts.length > 0 ? parts.join(', ') : '—';
}

function text(value: Maskable): string {
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
