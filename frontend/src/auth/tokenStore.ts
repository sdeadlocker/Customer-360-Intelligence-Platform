/**
 * Where the access and refresh tokens live between requests (task 12.1).
 *
 * The tokens are held in `sessionStorage`, not `localStorage`: a session token is cleared when the
 * tab closes, which matches the backend's short-lived-access-token, idle-timeout model (requirement
 * 12.10) and keeps a shared machine from leaking a book across browser restarts. This is a
 * development-provider convenience — production uses OIDC, where the enterprise IdP owns the session
 * — so the trade-off (a page reload keeps you logged in for the tab's life) is acceptable and
 * deliberate.
 *
 * The store is a thin, testable wrapper around `sessionStorage` with an in-memory fallback so the
 * client works under SSR or a locked-down environment where storage throws.
 */

export interface StoredTokens {
  readonly accessToken: string;
  readonly refreshToken: string;
}

const ACCESS_KEY = 'c360.access_token';
const REFRESH_KEY = 'c360.refresh_token';

function safeStorage(): Storage | null {
  try {
    if (typeof sessionStorage === 'undefined') {
      return null;
    }
    // Touch it: a private-mode or policy-locked browser can throw on access, not just on write.
    const probe = '__c360_probe__';
    sessionStorage.setItem(probe, '1');
    sessionStorage.removeItem(probe);
    return sessionStorage;
  } catch {
    return null;
  }
}

// In-memory fallback so a storage-less environment still holds a token for the tab's lifetime.
const memory = new Map<string, string>();

function read(key: string): string | null {
  const storage = safeStorage();
  if (storage !== null) {
    return storage.getItem(key);
  }
  return memory.get(key) ?? null;
}

function write(key: string, value: string): void {
  const storage = safeStorage();
  if (storage !== null) {
    storage.setItem(key, value);
    return;
  }
  memory.set(key, value);
}

function remove(key: string): void {
  const storage = safeStorage();
  if (storage !== null) {
    storage.removeItem(key);
  }
  memory.delete(key);
}

export const tokenStore = {
  get(): StoredTokens | null {
    const accessToken = read(ACCESS_KEY);
    const refreshToken = read(REFRESH_KEY);
    if (accessToken === null || refreshToken === null) {
      return null;
    }
    return { accessToken, refreshToken };
  },

  set(tokens: StoredTokens): void {
    write(ACCESS_KEY, tokens.accessToken);
    write(REFRESH_KEY, tokens.refreshToken);
  },

  clear(): void {
    remove(ACCESS_KEY);
    remove(REFRESH_KEY);
  },
};
