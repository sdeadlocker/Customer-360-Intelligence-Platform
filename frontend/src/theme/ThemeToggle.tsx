import { useTheme } from './ThemeContext';

/**
 * A compact light/dark toggle for the app chrome.
 *
 * It reports the *current* theme to assistive tech and flips on activation. The icon is decorative
 * (the accessible name carries the meaning), and the control uses the shared focus ring, so it is
 * keyboard-operable and visible on focus like every other control (requirement 16.1).
 */
export function ThemeToggle(): React.JSX.Element {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === 'dark';
  return (
    <button
      type="button"
      className="theme-toggle focus-ring"
      onClick={toggleTheme}
      aria-pressed={isDark}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      <span className="theme-toggle__icon" aria-hidden="true">
        {isDark ? '☀️' : '🌙'}
      </span>
      <span className="theme-toggle__label">{isDark ? 'Light' : 'Dark'}</span>
    </button>
  );
}
