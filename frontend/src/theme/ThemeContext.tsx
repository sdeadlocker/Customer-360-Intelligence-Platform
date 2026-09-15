import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

/**
 * Theme state: light (default) or dark, persisted and applied to the document element.
 *
 * The palette lives entirely in CSS tokens (`tokens.css`); this context only decides which set is
 * active by writing `data-theme` on `<html>`. Precedence, matching the CSS:
 *
 * 1. an explicit user choice (persisted in `localStorage`) always wins;
 * 2. with no stored choice, the OS `prefers-color-scheme` is followed (and tracked live);
 * 3. the built-in default is light.
 *
 * Writing `data-theme` only when there *is* an effective value keeps the CSS `:not([data-theme])`
 * OS-preference fallback working for a first visit; once the user toggles, the attribute is set and
 * their choice is authoritative.
 */

export type Theme = 'light' | 'dark';

interface ThemeContextValue {
  /** The theme currently applied to the document. */
  readonly theme: Theme;
  /** Set an explicit theme (persisted). */
  readonly setTheme: (theme: Theme) => void;
  /** Flip between light and dark. */
  readonly toggleTheme: () => void;
}

const ThemeCtx = createContext<ThemeContextValue | null>(null);

const STORAGE_KEY = 'c360.theme';

function readStored(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch {
    return null;
  }
}

function prefersDark(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  );
}

/** The theme to apply on first render: a stored choice, else the OS preference, else light. */
function initialTheme(): Theme {
  const stored = readStored();
  if (stored !== null) {
    return stored;
  }
  return prefersDark() ? 'dark' : 'light';
}

export function ThemeProvider({ children }: { readonly children: ReactNode }): React.JSX.Element {
  const [theme, setThemeState] = useState<Theme>(initialTheme);
  // Whether the user has made an explicit choice; until they do, we follow the OS preference live.
  const [explicit, setExplicit] = useState<boolean>(() => readStored() !== null);

  // Reflect the active theme onto <html data-theme> so the CSS token set switches. Only written when
  // the user has chosen, so a first visit keeps the CSS OS-preference fallback (`:not([data-theme])`).
  useEffect(() => {
    const root = document.documentElement;
    if (explicit) {
      root.setAttribute('data-theme', theme);
    } else {
      root.removeAttribute('data-theme');
    }
  }, [theme, explicit]);

  // Track the OS preference while the user has not chosen, so the app follows a system theme change.
  useEffect(() => {
    if (explicit || typeof window.matchMedia !== 'function') {
      return undefined;
    }
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (event: MediaQueryListEvent): void => {
      setThemeState(event.matches ? 'dark' : 'light');
    };
    media.addEventListener('change', onChange);
    return () => {
      media.removeEventListener('change', onChange);
    };
  }, [explicit]);

  const setTheme = useCallback((next: Theme): void => {
    setThemeState(next);
    setExplicit(true);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // A storage failure (private mode) is non-fatal: the theme still applies for this session.
    }
  }, []);

  const toggleTheme = useCallback((): void => {
    setThemeState((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark';
      setExplicit(true);
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, setTheme, toggleTheme }),
    [theme, setTheme, toggleTheme],
  );

  return <ThemeCtx.Provider value={value}>{children}</ThemeCtx.Provider>;
}

/** Access the theme. Falls back to a light, no-op implementation outside a provider (safe in tests). */
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeCtx);
  if (ctx === null) {
    return { theme: 'light', setTheme: () => undefined, toggleTheme: () => undefined };
  }
  return ctx;
}
