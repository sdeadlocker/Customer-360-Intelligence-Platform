import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * A top-level error boundary (task 12.1).
 *
 * A render-time exception anywhere below this boundary is caught and shown as a recoverable error
 * screen rather than a blank page — the dashboard composes many independent modules, and one
 * throwing during render must not take the whole app down. The fallback offers a reload rather than
 * pretending nothing happened. It deliberately shows only the error message, never a stack with
 * potentially sensitive interpolated values, on screen.
 */

interface Props {
  readonly children: ReactNode;
  readonly fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface State {
  readonly error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Log for local diagnosis; RUM/tracing owns anything that leaves the browser.
    console.error('Unhandled UI error', error, info.componentStack);
  }

  private readonly reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (error !== null) {
      if (this.props.fallback) {
        return this.props.fallback(error, this.reset);
      }
      return (
        <div role="alert" className="app-error">
          <h1>Something went wrong</h1>
          <p>The page hit an unexpected error and cannot continue.</p>
          <p className="app-error__detail">{error.message}</p>
          <button
            type="button"
            className="button"
            onClick={() => {
              window.location.reload();
            }}
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
