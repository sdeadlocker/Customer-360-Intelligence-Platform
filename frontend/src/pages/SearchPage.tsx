import { useCallback, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { AppFooter } from '../components/AppFooter';
import { AskPanel } from '../features/ai/AskPanel';
import { CitationProvider } from '../features/ai/CitationContext';
import { PassageViewer } from '../features/ai/PassageViewer';
import { MoneyInMotionPanel, MONEY_IN_MOTION_ANCHOR } from '../features/revenue/MoneyInMotion';
import { RevenueHero } from '../features/revenue/RevenueHero';
import { CustomerSearch } from '../features/search/CustomerSearch';
import { ThemeToggle } from '../theme/ThemeToggle';

/**
 * A collapsible landing section: a slim launcher bar that expands to reveal its content and can be
 * collapsed again. Used for customer search so it opens by a click and folds away, keeping the
 * revenue sections visible without scrolling — the same collapse/expand model the Ask panel uses.
 */
function CollapsibleSection({
  title,
  hint,
  glyph,
  defaultOpen = false,
  children,
}: {
  readonly title: string;
  readonly hint: string;
  readonly glyph: string;
  readonly defaultOpen?: boolean;
  readonly children: React.ReactNode;
}): React.JSX.Element {
  const [open, setOpen] = useState(defaultOpen);

  if (!open) {
    return (
      <section className="module collapsible collapsible--closed">
        <button
          type="button"
          className="ask-launcher focus-ring"
          onClick={() => {
            setOpen(true);
          }}
        >
          <span className="ask-launcher__icon" aria-hidden="true">
            {glyph}
          </span>
          <span className="ask-launcher__text">
            <span className="ask-launcher__title">{title}</span>
            <span className="ask-launcher__hint">{hint}</span>
          </span>
          <span className="ask-launcher__cta" aria-hidden="true">
            Open
          </span>
        </button>
      </section>
    );
  }

  return (
    <section className="module collapsible">
      <div className="module__head collapsible__head">
        <h2 className="module__title">{title}</h2>
        <button
          type="button"
          className="button button--subtle focus-ring"
          onClick={() => {
            setOpen(false);
          }}
        >
          Collapse
        </button>
      </div>
      <div className="module__body">{children}</div>
    </section>
  );
}

/**
 * The landing page after login (requirements 3.1–3.6, 11.1): the primary entry point.
 *
 * The page grew past the point where a single stacked column reads well — the priced opportunity
 * pipeline, the money-in-motion feed, customer search and the signals worklist are four substantial
 * surfaces — so it uses the **same left navigation rail as the customer dashboard**: the shared
 * `dash-nav` chrome and the same scroll-to-section behaviour. A user moving between the landing page
 * and a customer 360 meets one navigation model, not two.
 *
 * Unlike the dashboard there are no view modes here. The landing page is a daily briefing, so every
 * section is always mounted and the rail is purely a jump-to: four surfaces are few enough to read
 * straight through, and hiding any of them would only obscure the day's work.
 *
 * **Where Ask lives, and why.** The universal (cross-customer) Ask panel is *not* a nav section. It
 * is pinned full-width directly beneath the header, above the rail and the content, which is exactly
 * how the dashboard pins its customer-scoped Ask panel under the filter bar. Three reasons:
 *
 *  1. Requirement 11.1 asks for plain-language questions "immediately after login, before opening any
 *     profile" — so it must be reachable with zero navigation and zero scrolling.
 *  2. It is the one surface that is not about a section: the agent resolves which customer a question
 *     concerns across the whole entitled book, so navigating "away" from it would be meaningless.
 *  3. It matches the dashboard's placement, so the muscle memory transfers.
 *
 * The rail still advertises it — a help block and an "Ask AI" jump button at the top of the nav — so
 * discoverability does not depend on the panel happening to be in view.
 *
 * The Ask panel is wrapped in {@link CitationProvider} + {@link PassageViewer} so a knowledge citation
 * in an answer opens its source passage inline, and a fact citation opens the cited customer's 360.
 */

interface LandingSection {
  /** Stable id: the nav anchor, the `#section-${id}` scroll target and the `data-section` hook. */
  readonly id: string;
  readonly label: string;
  readonly group: 'Revenue' | 'Customers';
  readonly node: React.ReactNode;
}

const SECTION_GROUPS: readonly LandingSection['group'][] = ['Revenue', 'Customers'];

/** A decorative glyph per section, rendered into the nav via `data-icon` exactly as the dashboard does. */
const SECTION_ICONS: Readonly<Record<string, string>> = {
  pipeline: '📈',
  'money-in-motion': '💸',
  search: '🔍',
};

/** The id of the pinned Ask panel, so the nav's "Ask AI" button can jump to it. */
const ASK_ANCHOR = 'landing-ask';

export function SearchPage(): React.JSX.Element {
  const navigate = useNavigate();
  const { principal, logout } = useAuth();

  // Every section is always rendered; `active` only tracks which rail entry to mark as current.
  // Defaults to the lead section (customer search).
  const [active, setActive] = useState<string>('search');

  const openCustomer = useCallback(
    (customerId: string) => {
      void navigate(`/customers/${encodeURIComponent(customerId)}`);
    },
    [navigate],
  );

  const scrollToMoneyInMotion = useCallback(() => {
    setActive(MONEY_IN_MOTION_ANCHOR);
    document
      .getElementById(`section-${MONEY_IN_MOTION_ANCHOR}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  // Order matters: finding a customer is the most-used action, so it leads — but as a collapsed
  // launcher, so it takes one line until clicked and the revenue sections below stay in view. The
  // pipeline follows, then money in motion. (The signals worklist was removed from this page.)
  const sections = useMemo<readonly LandingSection[]>(
    () => [
      {
        id: 'search',
        label: 'Find a customer',
        group: 'Customers',
        node: (
          <CollapsibleSection
            title="Find a customer"
            hint="Search by name, ID, email, phone or card last 4"
            glyph="🔍"
          >
            <CustomerSearch onSelect={openCustomer} />
          </CollapsibleSection>
        ),
      },
      {
        id: 'pipeline',
        label: 'Opportunity pipeline',
        group: 'Revenue',
        node: <RevenueHero onOpenUrgent={scrollToMoneyInMotion} />,
      },
      {
        id: MONEY_IN_MOTION_ANCHOR,
        label: 'Money in motion',
        group: 'Revenue',
        node: <MoneyInMotionPanel onOpenCustomer={openCustomer} />,
      },
    ],
    [openCustomer, scrollToMoneyInMotion],
  );

  const selectSection = useCallback((id: string) => {
    setActive(id);
    document
      .getElementById(`section-${id}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  return (
    <>
      <a className="skip-link focus-ring" href="#search-main">
        Skip to your workspace
      </a>
      {/* Soft decorative background shapes (presentational, non-interactive). */}
      <div className="search-page__decor" aria-hidden="true">
        <span className="search-page__blob search-page__blob--1" />
        <span className="search-page__blob search-page__blob--2" />
        <span className="search-page__blob search-page__blob--3" />
        <span className="search-page__dots" />
      </div>

      <div className="search-page">
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

        {/* The universal Ask panel: pinned, full width, above the rail and the sections, so a plain
            language question needs no navigation at all (requirement 11.1). Not a nav section — the
            agent resolves the customer itself, so there is nothing to navigate away from. */}
        <div className="landing__ask" id={ASK_ANCHOR}>
          <CitationProvider
            onInspectFactWithoutAnchor={(citation) => {
              // There is no widget on this page to scroll to, so a fact citation opens the cited
              // customer's full 360 view, carrying the field in the hash for a deep link.
              void navigate(
                `/customers/${encodeURIComponent(citation.entity_id)}#${encodeURIComponent(
                  `${citation.entity_type}:${citation.field}`,
                )}`,
              );
            }}
          >
            <AskPanel collapsible />
            <PassageViewer />
          </CitationProvider>
        </div>

        {/* The same two-column rail-plus-content grid the dashboard uses. */}
        <div className="dashboard__layout">
          <nav className="dash-nav" aria-label="Workspace sections">
            <div className="dash-nav__brand">
              <span className="dash-nav__brand-mark" aria-hidden="true">
                ◐
              </span>
              <span className="dash-nav__brand-text">
                <span className="dash-nav__brand-name">Workspace</span>
                <span className="dash-nav__brand-tag">Revenue &amp; worklist</span>
              </span>
            </div>

            {/* Points at the pinned Ask panel above, mirroring the dashboard's help block. */}
            <div className="dash-nav__help" aria-hidden="true">
              <p className="dash-nav__help-title">
                Ask anything <span className="dash-nav__help-wave">👋</span>
              </p>
              <p className="dash-nav__help-text">
                The AI panel is pinned at the top — ask about any customer you are entitled to.
              </p>
            </div>

            <button
              type="button"
              className="dash-nav__link focus-ring"
              data-icon="✨"
              onClick={() => {
                document
                  .getElementById(ASK_ANCHOR)
                  ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
            >
              Ask AI
            </button>

            {SECTION_GROUPS.map((group) => (
              <div key={group} className="dash-nav__group">
                <p className="dash-nav__group-title">{group}</p>
                <ul className="dash-nav__list">
                  {sections
                    .filter((section) => section.group === group)
                    .map((section) => (
                      <li key={section.id}>
                        <button
                          type="button"
                          className={`dash-nav__link focus-ring${active === section.id ? ' dash-nav__link--active' : ''}`}
                          aria-current={active === section.id ? 'true' : undefined}
                          data-icon={SECTION_ICONS[section.id] ?? '•'}
                          onClick={() => {
                            selectSection(section.id);
                          }}
                        >
                          {section.label}
                        </button>
                      </li>
                    ))}
                </ul>
              </div>
            ))}

            {/* Quick tips, carried over from the old right-hand hero panel. Kept as real, readable
                content rather than decoration — only the glyphs are hidden. */}
            <div className="dash-nav__tips">
              <p className="dash-nav__group-title">Quick tips</p>
              <ul className="dash-nav__tip-list">
                <li className="dash-nav__tip">
                  <span aria-hidden="true">✨</span>
                  <span>Ask the AI about risks, finances or eligibility</span>
                </li>
                <li className="dash-nav__tip">
                  <span aria-hidden="true">🔍</span>
                  <span>Search by name, ID, email, phone or card last 4</span>
                </li>
                <li className="dash-nav__tip">
                  <span aria-hidden="true">🛡️</span>
                  <span>Results are always scoped to your entitlements</span>
                </li>
              </ul>
            </div>
          </nav>

          <main id="search-main" className="landing__main">
            {sections.map((section) => (
              <div
                key={section.id}
                id={`section-${section.id}`}
                className="landing-cell"
                data-section={section.id}
              >
                {section.node}
              </div>
            ))}
          </main>
        </div>
      </div>
      <AppFooter />
    </>
  );
}
