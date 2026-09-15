import { beforeEach, describe, expect, it } from 'vitest';

import { tokenStore } from './tokenStore';

describe('tokenStore', () => {
  beforeEach(() => {
    sessionStorage.clear();
    tokenStore.clear();
  });

  it('returns null when nothing is stored', () => {
    expect(tokenStore.get()).toBeNull();
  });

  it('round-trips a token pair', () => {
    tokenStore.set({ accessToken: 'a', refreshToken: 'r' });
    expect(tokenStore.get()).toEqual({ accessToken: 'a', refreshToken: 'r' });
  });

  it('clears both tokens', () => {
    tokenStore.set({ accessToken: 'a', refreshToken: 'r' });
    tokenStore.clear();
    expect(tokenStore.get()).toBeNull();
  });

  it('returns null when only one token is present', () => {
    sessionStorage.setItem('c360.access_token', 'a');
    expect(tokenStore.get()).toBeNull();
  });
});
