/**
 * Coordinates a full-dashboard capture for the PDF export.
 *
 * The PDF export clones the live `#dashboard-main` grid so the report mirrors the on-screen charts.
 * But the dashboard defaults to "Spotlight" mode, which mounts only the one active section — so a
 * naive capture would miss every other card's charts. Rather than couple the exporter to React
 * state, the dashboard registers a "capture provider" here: a function that temporarily mounts every
 * section (as in "360° Cockpit"), lets the browser paint, runs a supplied capture callback against
 * the now-complete grid, and then restores the previous view.
 *
 * If no provider is registered (e.g. the export is triggered from somewhere without a dashboard, or
 * in a test), the caller falls back to capturing whatever is currently mounted.
 */

/**
 * A provider runs `capture` while every dashboard section is mounted, then restores the prior view.
 * It resolves with whatever `capture` returned.
 */
export type CaptureProvider = <T>(capture: () => T) => Promise<T>;

let provider: CaptureProvider | null = null;

/** Register the dashboard's full-render capture provider. Returns an unregister function. */
export function registerCaptureProvider(next: CaptureProvider): () => void {
  provider = next;
  return () => {
    if (provider === next) {
      provider = null;
    }
  };
}

/**
 * Run `capture` with every dashboard section mounted. When a provider is registered it renders all
 * sections first (and restores afterwards); otherwise it just runs `capture` against the current
 * DOM.
 */
export async function withAllSections<T>(capture: () => T): Promise<T> {
  if (provider === null) {
    return capture();
  }
  return provider(capture);
}
