import { Navigate, useLocation } from 'react-router-dom';

import { useAuth } from './AuthContext';

/**
 * The route guard (task 12.1, requirement 12.1).
 *
 * A protected route renders only for an authenticated principal. While the provider is still
 * resolving a stored token it shows a neutral loading state rather than flashing the login screen
 * (which would be jarring on every reload). An unauthenticated user is redirected to `/login`, with
 * the attempted location remembered so login can send them back.
 */
export function RequireAuth({ children }: { children: React.ReactNode }): React.JSX.Element {
  const { status } = useAuth();
  const location = useLocation();

  if (status === 'initializing') {
    return (
      <div className="app-loading" role="status" aria-live="polite">
        Restoring your session…
      </div>
    );
  }

  if (status === 'unauthenticated') {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <>{children}</>;
}
