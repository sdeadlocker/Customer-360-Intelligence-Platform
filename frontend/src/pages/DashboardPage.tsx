import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { useNavigate, useParams } from 'react-router-dom';

import { registerCaptureProvider } from '../features/dashboard/exportCapture';

import { ApiError } from '../api/client';
import { AppFooter } from '../components/AppFooter';
import { request360, type Dashboard360 } from '../features/dashboard/dashboardData';
import { DashboardHeader } from '../features/dashboard/DashboardHeader';
import { FilterProvider } from '../features/dashboard/filters';
import { AiExperience } from '../features/ai/AiExperience';
import { AskPanel } from '../features/ai/AskPanel';
import { CitationProvider } from '../features/ai/CitationContext';
import { PassageViewer } from '../features/ai/PassageViewer';
import { ContactWidget } from '../features/dashboard/widgets/ContactWidget';
import { EngagementWidget } from '../features/dashboard/widgets/EngagementWidget';
import {
  ExpenseWidget,
  ExpenseCategoryWidget,
  ExpenseTrendWidget,
} from '../features/dashboard/widgets/ExpenseWidget';
import { FinancialWidget } from '../features/dashboard/widgets/FinancialWidget';
import { JourneyWidget } from '../features/dashboard/widgets/JourneyWidget';
import { OffersWidget } from '../features/dashboard/widgets/OffersWidget';
import { ProfileWidget } from '../features/dashboard/widgets/ProfileWidget';
import {
  RelationshipWidget,
  HouseholdWidget,
} from '../features/dashboard/widgets/RelationshipWidget';
import { RiskWidget } from '../features/dashboard/widgets/RiskWidget';
import { ReportsPanel } from '../features/reports/ReportsPanel';
import { EconomicProfitWidget } from '../features/revenue/EconomicProfitWidget';
import { FeeRecoveryWidget } from '../features/revenue/FeeRecoveryWidget';
import { MoneyInMotionWidget } from '../features/revenue/MoneyInMotion';
import { WalletShareWidget } from '../features/revenue/WalletShareWidget';
import { mark } from '../rum/rum';

/**
 * The dashboard shell (task 12.1 + 12.4 + 12.5 assembled; Phase 13 widgets mounted).
 *
 * It fetches the composed 360 read model for the customer in the URL, then renders each domain as a
 * widget. Each widget owns its own `ModuleState` wrapper and derives its state from the envelope
 * meta scoped to its module: a module the aggregator failed to build shows `error`; one carrying
 * masked fields shows `restricted`; a present-but-empty module shows "no data"; the rest show
 * `ready`. Widgets whose data is not composed into the 360 payload (holdings, the relationship
 * graph, engagement) fetch their own entitlement-scoped, masked sub-resources.
 */

type LoadState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly message: string; readonly correlationId: string | null }
  | { readonly kind: 'ready'; readonly view: Dashboard360 };

export function DashboardPage(): React.JSX.Element {
  const { customerId = '' } = useParams();
  const navigate = useNavigate();
  const [state, setState] = useState<LoadState>({ kind: 'loading' });
  const rendered = useRef(false);

  useEffect(() => {
    rendered.current = false;
    const controller = new AbortController();
    // Reset to loading in a microtask so it is not a synchronous cascading render in the effect body.
    queueMicrotask(() => {
      setState({ kind: 'loading' });
    });
    void request360(customerId, controller.signal)
      .then((view) => {
        setState({ kind: 'ready', view });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        if (cause instanceof ApiError) {
          setState({ kind: 'error', message: cause.message, correlationId: cause.correlationId });
          return;
        }
        const message = cause instanceof Error ? cause.message : 'Unknown error';
        setState({ kind: 'error', message, correlationId: null });
      });
    return () => {
      controller.abort();
    };
  }, [customerId]);

  useEffect(() => {
    if (state.kind === 'ready' && !rendered.current) {
      rendered.current = true;
      mark('dashboard.meaningful_render');
    }
  }, [state.kind]);

  const changeCustomer = useCallback(() => {
    void navigate('/');
  }, [navigate]);

  return (
    <FilterProvider>
      <CitationProvider>
        <a className="skip-link focus-ring" href="#dashboard-main">
          Skip to customer data
        </a>
        <div className="dashboard">
          <DashboardHeader
            customerId={customerId}
            onChangeCustomer={changeCustomer}
            view={state.kind === 'ready' ? state.view : undefined}
          />
          {/* Ask AI, scoped to this customer, sits at the top — directly under the filter bar —
              always visible, not tucked into a nav section. Its fact citations resolve to the
              owning widget below via the shared CitationProvider. */}
          <div className="dashboard__ask">
            <AskPanel customerId={customerId} collapsible />
          </div>
          <DashboardBody customerId={customerId} state={state} />
        </div>
        <AppFooter />
        <PassageViewer />
      </CitationProvider>
    </FilterProvider>
  );
}

function DashboardBody({
  customerId,
  state,
}: {
  readonly customerId: string;
  readonly state: LoadState;
}): React.JSX.Element {
  // A whole-page failure (customer not found, unreachable API) is one alert, not eight broken slots.
  if (state.kind === 'error') {
    return (
      <main id="dashboard-main" className="dashboard__grid" aria-busy={false}>
        <div role="alert" className="module module__error">
          <h2>Could not load customer {customerId}</h2>
          <p>{state.message}</p>
          {state.correlationId !== null && <p className="mono">Reference: {state.correlationId}</p>}
        </div>
      </main>
    );
  }

  if (state.kind === 'loading') {
    return (
      <main id="dashboard-main" className="dashboard__grid" aria-busy>
        <p role="status" className="module__loading">
          Loading customer…
        </p>
      </main>
    );
  }

  return <DashboardSections customerId={customerId} view={state.view} />;
}

/**
 * A dashboard section: a stable id used for the left-nav anchor and the focus filter, a nav label,
 * a grid-size hint (how much room the card wants when everything is shown), and the rendered widget.
 * Grouped so the nav can show headings without hard-coding the section list twice.
 */
type SectionSize = 'sm' | 'md' | 'lg' | 'xl';

interface DashboardSection {
  readonly id: string;
  readonly label: string;
  readonly group: 'Overview' | 'Revenue' | 'Intelligence' | 'AI insights';
  readonly size: SectionSize;
  readonly node: React.ReactNode;
}

/**
 * Nav group order. Revenue sits directly after Overview, ahead of Intelligence: the four plays
 * (Phase 22) answer "what is this relationship worth and what is it leaving on the table", which is
 * the question an RM opens a customer to act on.
 */
const SECTION_GROUPS: readonly DashboardSection['group'][] = [
  'Overview',
  'Revenue',
  'Intelligence',
  'AI insights',
];

/**
 * A decorative glyph per section, keyed by the section id. Purely presentational — it is rendered
 * into the nav via a `data-icon` attribute (CSS draws it) so it adds visual identity to the menu
 * without changing any navigation behaviour, labels, ordering or the sections array.
 */
const SECTION_ICONS: Readonly<Record<string, string>> = {
  profile: '👤',
  contact: '✉️',
  financial: '💰',
  'economic-profit': '📐',
  'wallet-share': '🏦',
  'money-in-motion': '💸',
  'fee-recovery': '🧾',
  expenses: '📊',
  'expenses-category': '🍩',
  'expenses-trend': '📈',
  risk: '🛡️',
  offers: '🎁',
  journey: '🧭',
  relationships: '🕸️',
  household: '🏠',
  engagement: '📇',
  ai: '✨',
  reports: '📄',
};

/**
 * The dashboard layout: a left navigation rail beside the cards.
 *
 * Two view modes (task: left nav + focus/all toggle):
 *  - **all** — every card in a controlled grid, sized by importance; the nav scrolls to a card.
 *  - **focus** — one section at a time; the nav selects which. Financial overview and the AI
 *    summary are the wide, important cards; profile/contact/risk are compact.
 */
function DashboardSections({
  customerId,
  view,
}: {
  readonly customerId: string;
  readonly view: Dashboard360;
}): React.JSX.Element {
  // Default to Spotlight (one section at a time) focused on Profile, rather than the all-cards
  // 360° Cockpit — the profile is the natural first thing to read on opening a customer.
  const [mode, setMode] = useState<'all' | 'focus'>('focus');
  const [active, setActive] = useState<string>('profile');
  // Card density in the all-cards (Cockpit) view: 'compact' shrinks every card so the whole 360 fits
  // on roughly one screen (an at-a-glance overview); 'comfortable' is the full, detailed layout.
  const [density, setDensity] = useState<'comfortable' | 'compact'>('comfortable');
  // When true, every section is mounted regardless of `mode` — used only transiently while the PDF
  // export captures the full grid (with all charts) even though the default view is Spotlight.
  const [forceAll, setForceAll] = useState(false);

  const sections = useMemo<readonly DashboardSection[]>(
    () => [
      {
        id: 'profile',
        label: 'Profile',
        group: 'Overview',
        size: 'sm',
        node: <ProfileWidget view={view} />,
      },
      {
        id: 'contact',
        label: 'Contact',
        group: 'Overview',
        size: 'sm',
        node: <ContactWidget view={view} />,
      },
      {
        id: 'financial',
        label: 'Financial overview',
        group: 'Overview',
        size: 'lg',
        node: <FinancialWidget view={view} customerId={customerId} />,
      },
      // ---------------------------------------------------------------- Revenue (Phase 22)
      // Ordered by the sequence the plays earn their keep: what the relationship is worth, what is
      // held elsewhere, what is movable right now, and what is billable today.
      {
        id: 'economic-profit',
        label: 'Economic profit',
        group: 'Revenue',
        size: 'md',
        node: <EconomicProfitWidget customerId={customerId} />,
      },
      {
        id: 'wallet-share',
        label: 'Wallet share',
        group: 'Revenue',
        size: 'lg',
        node: <WalletShareWidget customerId={customerId} />,
      },
      {
        id: 'money-in-motion',
        label: 'Money in motion',
        group: 'Revenue',
        size: 'md',
        node: <MoneyInMotionWidget customerId={customerId} />,
      },
      {
        id: 'fee-recovery',
        label: 'Fee recovery',
        group: 'Revenue',
        size: 'md',
        node: <FeeRecoveryWidget customerId={customerId} />,
      },
      {
        id: 'expenses',
        label: 'Expense analytics',
        group: 'Intelligence',
        size: 'sm',
        node: <ExpenseWidget view={view} />,
      },
      {
        id: 'expenses-category',
        label: 'Spend by category',
        group: 'Intelligence',
        size: 'md',
        node: <ExpenseCategoryWidget view={view} />,
      },
      {
        id: 'expenses-trend',
        label: 'Monthly spend trend',
        group: 'Intelligence',
        size: 'md',
        node: <ExpenseTrendWidget view={view} />,
      },
      {
        id: 'risk',
        label: 'Risk',
        group: 'Intelligence',
        size: 'sm',
        node: <RiskWidget view={view} />,
      },
      {
        id: 'offers',
        label: 'Offers',
        group: 'Intelligence',
        size: 'md',
        node: <OffersWidget view={view} />,
      },
      {
        id: 'journey',
        label: 'Journey',
        group: 'Intelligence',
        size: 'md',
        node: <JourneyWidget view={view} />,
      },
      {
        id: 'relationships',
        label: 'Relationships',
        group: 'Intelligence',
        size: 'md',
        node: <RelationshipWidget customerId={customerId} />,
      },
      {
        id: 'household',
        label: 'Household',
        group: 'Intelligence',
        size: 'sm',
        node: <HouseholdWidget customerId={customerId} />,
      },
      {
        id: 'engagement',
        label: 'Engagement history',
        group: 'Intelligence',
        size: 'md',
        node: <EngagementWidget customerId={customerId} />,
      },
      {
        id: 'ai',
        label: 'AI insights',
        group: 'AI insights',
        size: 'xl',
        node: <AiExperience customerId={customerId} />,
      },
      {
        id: 'reports',
        label: 'Reports',
        group: 'AI insights',
        size: 'lg',
        node: <ReportsPanel customerId={customerId} />,
      },
    ],
    [customerId, view],
  );

  const selectSection = useCallback(
    (id: string) => {
      setActive(id);
      if (mode === 'all') {
        // Scroll the card into view within the all-cards layout.
        requestAnimationFrame(() => {
          document.getElementById(`section-${id}`)?.scrollIntoView({
            behavior: 'smooth',
            block: 'start',
          });
        });
      }
    },
    [mode],
  );

  const visible =
    mode === 'focus' && !forceAll ? sections.filter((s) => s.id === active) : sections;

  // Register the PDF-export capture provider: mount every section, let the browser paint, run the
  // capture, then restore Spotlight. This is why the exported PDF includes every card's charts even
  // though the on-screen default shows one section at a time.
  useEffect(() => {
    return registerCaptureProvider(async (capture) => {
      flushSync(() => {
        setForceAll(true);
      });
      // Wait two animation frames so the newly-mounted cards (and their inline SVG charts) paint
      // before we clone the grid.
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            resolve();
          }),
        );
      });
      try {
        return capture();
      } finally {
        setForceAll(false);
      }
    });
  }, []);

  return (
    <div className="dashboard__layout">
      <nav className="dash-nav" aria-label="Dashboard sections">
        <div className="dash-nav__brand">
          <span className="dash-nav__brand-mark" aria-hidden="true">
            ◐
          </span>
          <span className="dash-nav__brand-text">
            <span className="dash-nav__brand-name">Customer 360</span>
            <span className="dash-nav__brand-tag">Intelligence Platform</span>
          </span>
        </div>

        {/* A friendly helper block (presentational) pointing to the Ask AI panel at the top of the
            page, in the spirit of the reference dashboard's "need help?" prompt. */}
        <div className="dash-nav__help" aria-hidden="true">
          <p className="dash-nav__help-title">
            Hey, need help? <span className="dash-nav__help-wave">👋</span>
          </p>
          <p className="dash-nav__help-text">Just ask the AI anything about this customer.</p>
        </div>

        {/* Three view choices: Spotlight and Compact on the top row, 360° Cockpit below.
            - Spotlight  → one section at a time.
            - Compact    → all cards, shrunk to an at-a-glance overview (all mode + compact density).
            - 360° Cockpit → all cards, full detail (all mode + comfortable density). */}
        <div className="dash-nav__modes" role="group" aria-label="View mode">
          <button
            type="button"
            className={`dash-nav__mode focus-ring${mode === 'focus' ? ' dash-nav__mode--active' : ''}`}
            aria-pressed={mode === 'focus'}
            onClick={() => {
              setMode('focus');
            }}
          >
            Spotlight
          </button>
          <button
            type="button"
            className={`dash-nav__mode focus-ring${mode === 'all' && density === 'compact' ? ' dash-nav__mode--active' : ''}`}
            aria-pressed={mode === 'all' && density === 'compact'}
            onClick={() => {
              setMode('all');
              setDensity('compact');
            }}
          >
            Compact
          </button>
          <button
            type="button"
            className={`dash-nav__mode dash-nav__mode--wide focus-ring${mode === 'all' && density === 'comfortable' ? ' dash-nav__mode--active' : ''}`}
            aria-pressed={mode === 'all' && density === 'comfortable'}
            onClick={() => {
              setMode('all');
              setDensity('comfortable');
            }}
          >
            360° Cockpit
          </button>
        </div>

        {SECTION_GROUPS.map((group) => (
          <div key={group} className="dash-nav__group">
            <p className="dash-nav__group-title">{group}</p>
            <ul className="dash-nav__list">
              {sections
                .filter((s) => s.group === group)
                .map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      className={`dash-nav__link focus-ring${active === s.id ? ' dash-nav__link--active' : ''}`}
                      aria-current={active === s.id ? 'true' : undefined}
                      data-icon={SECTION_ICONS[s.id] ?? '•'}
                      onClick={() => {
                        selectSection(s.id);
                      }}
                    >
                      {s.label}
                    </button>
                  </li>
                ))}
            </ul>
          </div>
        ))}

        <div className="dash-nav__promo" aria-hidden="true">
          <span className="dash-nav__promo-illustration">🤖</span>
          <span className="dash-nav__promo-title">AI Copilot</span>
          <span className="dash-nav__promo-text">
            Unified customer intelligence, powered by AI.
          </span>
        </div>
      </nav>

      <main
        id="dashboard-main"
        className={`dashboard__grid dashboard__grid--${mode}${
          mode === 'all' && density === 'compact' ? ' dashboard__grid--compact' : ''
        }`}
        aria-busy={false}
      >
        {visible.map((s) => (
          <div
            key={s.id}
            id={`section-${s.id}`}
            className={`dash-cell dash-cell--${s.size}`}
            data-section={s.id}
          >
            {s.node}
          </div>
        ))}
      </main>
    </div>
  );
}
