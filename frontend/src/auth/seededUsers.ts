import type { Role } from '../api/types';

/**
 * The six seeded development logins, mirroring the backend's local provider
 * (`backend/src/c360/security/local_provider.py`). The login screen offers these as one-click
 * sign-ins so the "six role logins work" phase gate is exercisable without anyone memorising a
 * development password. These are fixed local-dev credentials by design — production uses OIDC,
 * where credentials never touch this app — so surfacing them here is not a leak.
 */

export interface SeededUser {
  readonly role: Role;
  readonly label: string;
  readonly username: string;
  readonly password: string;
  /** A one-line description of what this role can see, for the login card. */
  readonly scope: string;
}

export const SEEDED_USERS: readonly SeededUser[] = [
  {
    role: 'RM',
    label: 'Relationship Manager',
    username: 'rm.taylor',
    password: 'rm-dev-password',
    scope: 'A restricted book of customers',
  },
  {
    role: 'WEALTH_ADVISOR',
    label: 'Wealth Advisor',
    username: 'wealth.morgan',
    password: 'wealth-dev-password',
    scope: 'HNW, UHNW and affluent segments',
  },
  {
    role: 'CONTACT_CENTER',
    label: 'Contact Center',
    username: 'contact.jordan',
    password: 'contact-dev-password',
    scope: 'All customers, masked contact detail',
  },
  {
    role: 'BRANCH',
    label: 'Branch',
    username: 'branch.casey',
    password: 'branch-dev-password',
    scope: 'All customers',
  },
  {
    role: 'RISK',
    label: 'Risk (full access)',
    username: 'risk.riley',
    password: 'risk-dev-password',
    scope: 'All customers — full access to every field',
  },
  {
    role: 'MARKETING',
    label: 'Marketing (restricted)',
    username: 'marketing.avery',
    password: 'marketing-dev-password',
    scope: 'Segment-scoped — DOB, contact, risk & finances hidden',
  },
];
