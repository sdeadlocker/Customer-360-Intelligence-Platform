import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { SEEDED_USERS, type SeededUser } from '../auth/seededUsers';
import type { Role } from '../api/types';
import { ThemeToggle } from '../theme/ThemeToggle';

/**
 * The login screen (task 12.1, requirements 12.1, 12.2).
 *
 * Offers the six seeded roles as one-click sign-ins so the "six role logins work" phase gate is
 * exercisable without memorising a development password, and a manual username/password form beside
 * them. On success it returns the user to wherever the guard sent them from (or the home search).
 */

/** Read the `from` pathname the guard stashed in location state, tolerating an absent/odd shape. */
function redirectTarget(state: unknown): string {
  if (typeof state === 'object' && state !== null && 'from' in state) {
    const from = (state as { from?: unknown }).from;
    if (typeof from === 'object' && from !== null && 'pathname' in from) {
      const pathname = (from as { pathname?: unknown }).pathname;
      if (typeof pathname === 'string' && pathname !== '') {
        return pathname;
      }
    }
  }
  return '/';
}

/**
 * A decorative glyph per role. Purely presentational (aria-hidden) — it adds visual identity to the
 * role cards without changing any role data, labels, scopes or sign-in behaviour.
 */
function roleGlyph(role: Role): string {
  switch (role) {
    case 'RM':
      return '🤝';
    case 'WEALTH_ADVISOR':
      return '💎';
    case 'CONTACT_CENTER':
      return '🎧';
    case 'BRANCH':
      return '🏦';
    case 'RISK':
      return '🛡️';
    case 'MARKETING':
      return '📣';
    default:
      return '👤';
  }
}

/** The feature highlights shown in the hero. Presentational only. */
const HERO_HIGHLIGHTS: readonly { readonly icon: string; readonly label: string }[] = [
  { icon: '📊', label: 'Customer Insights' },
  { icon: '🤖', label: 'AI Recommendations' },
  { icon: '🛡️', label: 'Risk Analytics' },
  { icon: '🕸️', label: 'Relationship Intelligence' },
];

export function LoginPage(): React.JSX.Element {
  const { login, status } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const redirectTo = redirectTarget(location.state);

  const doLogin = async (user: string, pass: string, marker: string): Promise<void> => {
    setError(null);
    setPending(marker);
    try {
      await login(user, pass);
      void navigate(redirectTo, { replace: true });
    } catch (cause) {
      const message =
        cause instanceof ApiError
          ? cause.message
          : cause instanceof Error
            ? cause.message
            : 'Sign in failed';
      setError(message);
    } finally {
      setPending(null);
    }
  };

  return (
    <main className="login">
      {/* Soft decorative background shapes (aria-hidden, non-interactive). */}
      <div className="login__decor" aria-hidden="true">
        <span className="login__blob login__blob--1" />
        <span className="login__blob login__blob--2" />
        <span className="login__blob login__blob--3" />
        <span className="login__dots" />
      </div>

      <div className="login__card">
        {/* ---------------------------------------------------------------- hero */}
        <aside className="login__hero" aria-hidden="true">
          <div className="login__hero-shapes">
            <span className="login__hero-ring login__hero-ring--1" />
            <span className="login__hero-ring login__hero-ring--2" />
            <span className="login__hero-dot login__hero-dot--1" />
            <span className="login__hero-dot login__hero-dot--2" />
            <span className="login__hero-dot login__hero-dot--3" />
          </div>

          <div className="login__hero-brand">
            <span className="login__hero-mark">C360</span>
            <span className="login__hero-name">Customer 360</span>
          </div>

          <div className="login__hero-body">
            <div className="login__hero-illustration">
              {/* A lightweight banking / customer-analytics illustration. */}
              <svg viewBox="0 0 240 160" role="img" aria-hidden="true" className="login__hero-svg">
                <defs>
                  <linearGradient id="c360-bar" x1="0" y1="1" x2="0" y2="0">
                    <stop offset="0%" stopColor="rgba(255,255,255,0.35)" />
                    <stop offset="100%" stopColor="rgba(255,255,255,0.9)" />
                  </linearGradient>
                </defs>
                {/* dashboard panel */}
                <rect
                  x="16"
                  y="18"
                  width="208"
                  height="124"
                  rx="14"
                  fill="rgba(255,255,255,0.08)"
                  stroke="rgba(255,255,255,0.35)"
                />
                {/* analytics bars */}
                <rect x="34" y="92" width="18" height="34" rx="4" fill="url(#c360-bar)" />
                <rect x="60" y="74" width="18" height="52" rx="4" fill="url(#c360-bar)" />
                <rect x="86" y="58" width="18" height="68" rx="4" fill="url(#c360-bar)" />
                <rect x="112" y="80" width="18" height="46" rx="4" fill="url(#c360-bar)" />
                {/* trend line */}
                <polyline
                  points="40,66 68,54 96,40 124,52 152,34 180,28 206,22"
                  fill="none"
                  stroke="rgba(255,255,255,0.95)"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <circle cx="206" cy="22" r="4.5" fill="#ffd0e0" />
                <circle cx="152" cy="34" r="3.5" fill="#ffd9a8" />
                {/* customer donut */}
                <circle
                  cx="180"
                  cy="104"
                  r="20"
                  fill="none"
                  stroke="rgba(255,255,255,0.3)"
                  strokeWidth="7"
                />
                <circle
                  cx="180"
                  cy="104"
                  r="20"
                  fill="none"
                  stroke="#ffd0e0"
                  strokeWidth="7"
                  strokeDasharray="80 45"
                  strokeLinecap="round"
                  transform="rotate(-90 180 104)"
                />
              </svg>
            </div>

            <h2 className="login__hero-tagline">Unified Customer Intelligence powered by AI</h2>
            <p className="login__hero-lede">
              Bring every signal — relationships, risk and recommendations — into one enterprise
              view.
            </p>

            <ul className="login__hero-features">
              {HERO_HIGHLIGHTS.map((feature) => (
                <li key={feature.label} className="login__hero-feature">
                  <span className="login__hero-feature-icon">{feature.icon}</span>
                  <span>{feature.label}</span>
                </li>
              ))}
            </ul>
          </div>
        </aside>

        {/* ---------------------------------------------------------------- login panel */}
        <div className="login__panel">
          <div className="login__panel-top">
            <span className="login__brandmark" aria-hidden="true">
              C360
            </span>
            <ThemeToggle />
          </div>

          <div className="login__intro">
            <h1 className="login__title">
              Welcome Back 👋
              {/* The product name stays in the accessible heading name (visually part of the hero
                  branding) so assistive tech and the level-1 heading still identify the app. */}
              <span className="sr-only"> to the Customer 360 Intelligence Platform</span>
            </h1>
            <p className="login__subtitle">
              Sign in with a seeded role to explore the Customer 360 Intelligence Platform.
            </p>
          </div>

          {error !== null && (
            <p role="alert" className="login__error">
              {error}
            </p>
          )}

          <section aria-labelledby="roles-heading" className="login__roles">
            <h2 id="roles-heading" className="login__section-title">
              One-click roles
            </h2>
            <ul className="role-grid">
              {SEEDED_USERS.map((seeded) => (
                <RoleCard
                  key={seeded.role}
                  user={seeded}
                  busy={pending === seeded.role || status === 'initializing'}
                  onPick={() => void doLogin(seeded.username, seeded.password, seeded.role)}
                />
              ))}
            </ul>
          </section>

          <section aria-labelledby="manual-heading" className="login__manual">
            <h2 id="manual-heading" className="login__section-title">
              Or sign in manually
            </h2>
            <form
              className="login__form"
              onSubmit={(event) => {
                event.preventDefault();
                void doLogin(username, password, 'manual');
              }}
            >
              <label className="login__field">
                <span>Username</span>
                <input
                  type="text"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => {
                    setUsername(event.target.value);
                  }}
                  required
                />
              </label>
              <label className="login__field">
                <span>Password</span>
                <input
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value);
                  }}
                  required
                />
              </label>
              <button
                type="submit"
                className="button button--primary"
                disabled={pending === 'manual'}
              >
                {pending === 'manual' ? 'Signing in…' : 'Sign in'}
              </button>
            </form>
          </section>
        </div>
      </div>
    </main>
  );
}

function RoleCard({
  user,
  busy,
  onPick,
}: {
  readonly user: SeededUser;
  readonly busy: boolean;
  readonly onPick: () => void;
}): React.JSX.Element {
  return (
    <li className="role-card">
      <button type="button" className="role-card__button" onClick={onPick} disabled={busy}>
        <span className="role-card__icon" aria-hidden="true">
          {roleGlyph(user.role)}
        </span>
        <span className="role-card__text">
          <span className="role-card__label">{user.label}</span>
          <span className="role-card__scope">{user.scope}</span>
          <span className="role-card__user mono">{user.username}</span>
        </span>
      </button>
    </li>
  );
}
