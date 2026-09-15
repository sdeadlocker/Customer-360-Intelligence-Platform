import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import {
  ApiError,
  fetchMe,
  login as apiLogin,
  logout as apiLogout,
  setAuthLostHandler,
} from '../api/client';
import type { MeResponse } from '../api/types';
import { tokenStore } from './tokenStore';

/**
 * The authentication context (task 12.1).
 *
 * Holds the current principal — the `/me` payload, which carries the role that drives every
 * role-gated affordance and the entitlement summary — and exposes `login` / `logout`. On mount it
 * checks for a stored token and, if present, resolves the principal so a page reload does not force
 * a fresh login within the tab's life. When the API client signals that the session is
 * unrecoverable (a 401 that survives a refresh), the provider drops the principal so the router's
 * guard sends the user back to login.
 */

export type AuthStatus = 'initializing' | 'authenticated' | 'unauthenticated';

interface AuthState {
  readonly status: AuthStatus;
  readonly principal: MeResponse | null;
}

interface AuthContextValue extends AuthState {
  readonly login: (username: string, password: string) => Promise<void>;
  readonly logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [state, setState] = useState<AuthState>(() => ({
    // If there is no stored token there is nothing to resolve — start unauthenticated and skip the
    // initial `/me` round trip entirely.
    status: tokenStore.get() === null ? 'unauthenticated' : 'initializing',
    principal: null,
  }));

  // Guards a resolve from writing state after logout/unmount raced ahead of it.
  const activeRef = useRef(true);

  const clearSession = useCallback(() => {
    apiLogout();
    setState({ status: 'unauthenticated', principal: null });
  }, []);

  // Route back to login when the client reports the session is gone.
  useEffect(() => {
    setAuthLostHandler(() => {
      setState({ status: 'unauthenticated', principal: null });
    });
    return () => {
      setAuthLostHandler(null);
    };
  }, []);

  // Resolve the principal from a stored token on first mount.
  useEffect(() => {
    activeRef.current = true;
    if (tokenStore.get() === null) {
      return;
    }
    const controller = new AbortController();
    fetchMe(controller.signal)
      .then((envelope) => {
        if (activeRef.current) {
          setState({ status: 'authenticated', principal: envelope.data });
        }
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || !activeRef.current) {
          return;
        }
        // A stored-but-invalid token means the session is not usable — start clean at login.
        if (cause instanceof ApiError) {
          clearSession();
        } else {
          setState({ status: 'unauthenticated', principal: null });
        }
      });
    return () => {
      activeRef.current = false;
      controller.abort();
    };
  }, [clearSession]);

  const login = useCallback(async (username: string, password: string) => {
    await apiLogin(username, password);
    const envelope = await fetchMe();
    setState({ status: 'authenticated', principal: envelope.data });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ ...state, login, logout: clearSession }),
    [state, login, clearSession],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return value;
}
