/**
 * Application footer for the landing and dashboard pages.
 *
 * A full multi-column footer: brand/about blurb with social links, quick-link columns and a
 * contact-info column, followed by a compliance/copyright strip. Presentational chrome;
 * keyboard-operable and labelled.
 */

/** Build version, overridable at build time via `VITE_APP_VERSION`; falls back to the package version. */
const APP_VERSION: string = (import.meta.env.VITE_APP_VERSION as string | undefined) ?? '0.1.0';

const SUPPORT_EMAIL = 'support@customer360.example';
const YEAR = new Date().getFullYear();

export function AppFooter(): React.JSX.Element {
  return (
    <footer className="app-footer" aria-label="Site footer">
      <div className="app-footer__inner">
        <div className="app-footer__grid">
          {/* About */}
          <div className="app-footer__col app-footer__col--about">
            <h2 className="app-footer__heading">About us</h2>
            <p className="app-footer__blurb">
              Customer 360 unifies profiles, transactions, offers and interactions into a single
              intelligent view, giving teams the context they need to serve every customer with
              confidence.
            </p>
            <ul className="app-footer__social" aria-label="Social links">
              <li>
                <a href="#" aria-label="Facebook" aria-disabled="true">
                  <FacebookIcon />
                </a>
              </li>
              <li>
                <a href="#" aria-label="LinkedIn" aria-disabled="true">
                  <LinkedInIcon />
                </a>
              </li>
              <li>
                <a href="#" aria-label="Twitter" aria-disabled="true">
                  <TwitterIcon />
                </a>
              </li>
              <li>
                <a href="#" aria-label="YouTube" aria-disabled="true">
                  <YouTubeIcon />
                </a>
              </li>
            </ul>
          </div>

          {/* Useful Links */}
          <nav className="app-footer__col" aria-label="Useful links">
            <h2 className="app-footer__heading">Useful Links</h2>
            <ul className="app-footer__links">
              <li>
                <a href="#" aria-disabled="true">
                  About Us
                </a>
              </li>
              <li>
                <a href="#" aria-disabled="true">
                  FAQs
                </a>
              </li>
              <li>
                <a href="#" aria-disabled="true">
                  Privacy &amp; Policy
                </a>
              </li>
              <li>
                <a href="#" aria-disabled="true">
                  Terms &amp; Condition
                </a>
              </li>
              <li>
                <a href={`mailto:${SUPPORT_EMAIL}`}>Contact Us</a>
              </li>
            </ul>
          </nav>

          {/* Platform */}
          <nav className="app-footer__col" aria-label="Platform links">
            <h2 className="app-footer__heading">Platform</h2>
            <ul className="app-footer__links">
              <li>
                <a href="#" aria-disabled="true">
                  Dashboard
                </a>
              </li>
              <li>
                <a href="#" aria-disabled="true">
                  Search
                </a>
              </li>
              <li>
                <a href="#" aria-disabled="true">
                  Offers
                </a>
              </li>
              <li>
                <a href="#" aria-disabled="true">
                  Insights
                </a>
              </li>
              <li>
                <a href="/ready">Status</a>
              </li>
            </ul>
          </nav>

          {/* Contact Info */}
          <div className="app-footer__col">
            <h2 className="app-footer__heading">Contact Info</h2>
            <ul className="app-footer__contact">
              <li>
                <span className="app-footer__contact-icon" aria-hidden="true">
                  <LocationIcon />
                </span>
                <span>
                  288 Bishopsgate
                  <br />
                  London
                  <br />
                  EC2M 4QP
                </span>
              </li>
              <li>
                <span className="app-footer__contact-icon" aria-hidden="true">
                  <PhoneIcon />
                </span>
                <a href="tel:+12345677890">+1 234 567 7890</a>
              </li>
              <li>
                <span className="app-footer__contact-icon" aria-hidden="true">
                  <MailIcon />
                </span>
                <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>
              </li>
            </ul>
          </div>
        </div>

        <div className="app-footer__bar">
          <p className="app-footer__legal">
            <span aria-hidden="true">🔒</span> Confidential · access is logged &amp; audited · AI is
            decision support, not advice · © {YEAR} Customer 360
          </p>
          <span className="app-footer__ver mono">v{APP_VERSION}</span>
        </div>
      </div>
    </footer>
  );
}

/* --------------------------------------------------------------------------- inline icons */

function FacebookIcon(): React.JSX.Element {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M13.5 21v-8h2.7l.4-3.1h-3.1V7.9c0-.9.25-1.5 1.55-1.5H16.7V3.6c-.3 0-1.3-.1-2.45-.1-2.43 0-4.1 1.48-4.1 4.2v2.2H7.4V13h2.75v8h3.35z" />
    </svg>
  );
}

function LinkedInIcon(): React.JSX.Element {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M4.98 3.5A2.5 2.5 0 1 0 5 8.5a2.5 2.5 0 0 0-.02-5zM3 9h4v12H3V9zm6 0h3.8v1.64h.05c.53-1 1.83-2.05 3.77-2.05 4.03 0 4.78 2.65 4.78 6.1V21h-4v-5.4c0-1.29-.02-2.95-1.8-2.95-1.8 0-2.08 1.4-2.08 2.85V21H9V9z" />
    </svg>
  );
}

function TwitterIcon(): React.JSX.Element {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M22 5.9c-.7.32-1.5.53-2.3.63.83-.5 1.46-1.28 1.76-2.22-.78.46-1.64.8-2.55.98A4 4 0 0 0 12 8.9c0 .32.03.62.1.92A11.35 11.35 0 0 1 3.9 4.6a4 4 0 0 0 1.24 5.34c-.65-.02-1.26-.2-1.8-.5v.05a4 4 0 0 0 3.2 3.92c-.58.16-1.2.18-1.8.07a4 4 0 0 0 3.74 2.78A8.03 8.03 0 0 1 2 18.9a11.32 11.32 0 0 0 6.13 1.8c7.36 0 11.38-6.1 11.38-11.38v-.52c.78-.56 1.46-1.27 2-2.07z" />
    </svg>
  );
}

function YouTubeIcon(): React.JSX.Element {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M23 12s0-3.2-.4-4.7a2.5 2.5 0 0 0-1.76-1.77C19.3 5.1 12 5.1 12 5.1s-7.3 0-8.84.43A2.5 2.5 0 0 0 1.4 7.3C1 8.8 1 12 1 12s0 3.2.4 4.7a2.5 2.5 0 0 0 1.76 1.77C4.7 18.9 12 18.9 12 18.9s7.3 0 8.84-.43a2.5 2.5 0 0 0 1.76-1.77C23 15.2 23 12 23 12zM9.75 15.02V8.98L15.5 12l-5.75 3.02z" />
    </svg>
  );
}

function LocationIcon(): React.JSX.Element {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M12 2a7 7 0 0 0-7 7c0 5.25 7 13 7 13s7-7.75 7-13a7 7 0 0 0-7-7zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5z" />
    </svg>
  );
}

function PhoneIcon(): React.JSX.Element {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M6.6 10.8a15.5 15.5 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.24c1.1.37 2.3.57 3.5.57a1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.2.2 2.4.57 3.5a1 1 0 0 1-.25 1l-2.2 2.3z" />
    </svg>
  );
}

function MailIcon(): React.JSX.Element {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 4-8 5-8-5V6l8 5 8-5v2z" />
    </svg>
  );
}
