import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { AuthProvider } from './auth/AuthContext';
import { RequireAuth } from './auth/RequireAuth';
import { ErrorBoundary } from './components/ErrorBoundary';
import { DashboardPage } from './pages/DashboardPage';
import { LoginPage } from './pages/LoginPage';
import { SearchPage } from './pages/SearchPage';
import { ThemeProvider } from './theme/ThemeContext';

/**
 * The app shell (task 12.1).
 *
 * Composition, from the outside in: an error boundary so a render fault shows a recoverable screen
 * rather than a blank page; the auth provider that resolves and holds the principal; and the router.
 * `/login` is public; everything else is behind `RequireAuth`, which sends an unauthenticated user
 * to login and remembers where they were headed. `/` is the customer search, `/customers/:id` is the
 * dashboard shell.
 */
export function App(): React.JSX.Element {
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <BrowserRouter>
          <AuthProvider>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                path="/"
                element={
                  <RequireAuth>
                    <SearchPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/customers/:customerId"
                element={
                  <RequireAuth>
                    <DashboardPage />
                  </RequireAuth>
                }
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </AuthProvider>
        </BrowserRouter>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
