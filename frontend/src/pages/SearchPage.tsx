import { useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { AppFooter } from '../components/AppFooter';
import { AskPanel } from '../features/ai/AskPanel';
import { CitationProvider } from '../features/ai/CitationContext';
import { PassageViewer } from '../features/ai/PassageViewer';
import { CustomerSearch } from '../features/search/CustomerSearch';
import { SignalsWorklist } from '../features/signals/SignalsWorklist';
import { ThemeToggle } from '../theme/ThemeToggle';

/**
 * The landing page after login (requirements 3.1–3.6, 11.1): the primary entry point.
 *
 * Two ways in, side by side. The cross-customer "Ask anything" panel ({@link AskPanel} with no
 * `customerId`) lets a relationship manager query any customer they are entitled to — their
 * insights, risks, relationships, finances and other details — directly from the search screen,
 * without opening a profile first; the agent searches across all customer data to resolve the
 * question itself (backend task 9.6). Below it, {@link CustomerSearch} finds a specific customer to
 * open in the full 360 view: selecting a hit navigates to `/customers/:id`, where the dashboard
 * shell renders.
 *
 * The panel is wrapped in {@link CitationProvider} + {@link PassageViewer} so a knowledge citation
 * in an answer opens its source passage inline, exactly as it did on the dashboard.
 */
export function SearchPage(): React.JSX.Element {
  const navigate = useNavigate();
  const { principal, logout } = useAuth();

  return (
    <>
      <a className="skip-link focus-ring" href="#search-main">
        Skip to search
      </a>
      {/* Soft decorative background shapes (presentational, non-interactive). */}
      <div className="search-page__decor" aria-hidden="true">
        <span className="search-page__blob search-page__blob--1" />
        <span className="search-page__blob search-page__blob--2" />
        <span className="search-page__blob search-page__blob--3" />
        <span className="search-page__dots" />
      </div>

      <main id="search-main" className="search-page">
        <header className="search-page__header">
          <div className="search-page__identity">
            <span className="search-page__brandmark" aria-hidden="true">
              ◐
            </span>
            <div>
              <h1>Customer 360</h1>
              {principal !== null && (
                <p className="search-page__role">
                  Signed in as {principal.role} · scope {principal.entitlement.kind}
                </p>
              )}
            </div>
          </div>
          <div className="search-page__actions">
            <ThemeToggle />
            <button type="button" className="button" onClick={logout}>
              Sign out
            </button>
          </div>
        </header>

        <div className="search-page__layout">
          <div className="search-page__primary">
            <CitationProvider
              onInspectFactWithoutAnchor={(citation) => {
                // On the landing page there is no widget to scroll to, so a fact citation opens the
                // cited customer's full 360 view instead. The field is carried in the hash so the
                // dashboard can deep-link to it.
                void navigate(
                  `/customers/${encodeURIComponent(citation.entity_id)}#${encodeURIComponent(
                    `${citation.entity_type}:${citation.field}`,
                  )}`,
                );
              }}
            >
              <AskPanel />
              <PassageViewer />
            </CitationProvider>

            <CustomerSearch
              onSelect={(customerId) => {
                void navigate(`/customers/${encodeURIComponent(customerId)}`);
              }}
            />

            {/* The prioritized daily worklist (Phase 17): the platform's proactive, push side,
                sitting beside Ask-AI's pull side. Drilling a card opens that customer's 360. */}
            <SignalsWorklist
              onOpenCustomer={(customerId) => {
                void navigate(`/customers/${encodeURIComponent(customerId)}`);
              }}
            />
          </div>

          {/* Branded hero / tips panel — presentational, mirrors the login hero. */}
          <aside className="search-page__hero" aria-hidden="true">
            <div className="search-page__hero-illustration">
              <svg viewBox="0 0 200 150" className="search-page__hero-svg" role="img">
                <defs>
                  <linearGradient id="sp-card" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%" stopColor="rgba(255,255,255,0.9)" />
                    <stop offset="100%" stopColor="rgba(255,255,255,0.55)" />
                  </linearGradient>
                </defs>
                <rect
                  x="20"
                  y="26"
                  width="120"
                  height="86"
                  rx="12"
                  fill="rgba(255,255,255,0.12)"
                  stroke="rgba(255,255,255,0.4)"
                />
                <circle cx="46" cy="52" r="12" fill="url(#sp-card)" />
                <rect x="66" y="44" width="60" height="7" rx="3.5" fill="rgba(255,255,255,0.85)" />
                <rect x="66" y="57" width="40" height="6" rx="3" fill="rgba(255,255,255,0.5)" />
                <rect x="34" y="78" width="92" height="6" rx="3" fill="rgba(255,255,255,0.4)" />
                <rect x="34" y="90" width="70" height="6" rx="3" fill="rgba(255,255,255,0.3)" />
                {/* AI spark + magnifier */}
                <circle cx="150" cy="96" r="26" fill="rgba(255,255,255,0.14)" />
                <circle cx="150" cy="96" r="16" fill="none" stroke="#ffd0e0" strokeWidth="4" />
                <line
                  x1="162"
                  y1="108"
                  x2="176"
                  y2="122"
                  stroke="#ffd0e0"
                  strokeWidth="5"
                  strokeLinecap="round"
                />
                <text x="150" y="40" textAnchor="middle" fontSize="18" fill="#ffe08a">
                  ✦
                </text>
              </svg>
            </div>
            <h2 className="search-page__hero-title">Unified Customer Intelligence</h2>
            <p className="search-page__hero-lede">
              Ask anything in plain language, or jump straight to a customer&apos;s full 360 view.
            </p>
            <ul className="search-page__hero-tips">
              <li className="search-page__hero-tip">
                <span className="search-page__hero-tip-icon">✨</span>
                <span>Ask the AI about risks, finances or eligibility</span>
              </li>
              <li className="search-page__hero-tip">
                <span className="search-page__hero-tip-icon">🔍</span>
                <span>Search by name, ID, email, phone or card last 4</span>
              </li>
              <li className="search-page__hero-tip">
                <span className="search-page__hero-tip-icon">🛡️</span>
                <span>Results are always scoped to your entitlements</span>
              </li>
            </ul>
          </aside>
        </div>
      </main>
      <AppFooter />
    </>
  );
}
