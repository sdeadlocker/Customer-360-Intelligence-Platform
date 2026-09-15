import type { CustomerSearchHit } from '../../api/types';

/**
 * The recently-viewed list (requirement 3.6): the last ten customers the user opened, most-recent
 * first, kept per browser in `localStorage`.
 *
 * This is a convenience list, not entitlement state — it only ever holds customers the user already
 * opened, so it discloses nothing they were not shown. It stores the same thin hit shape the search
 * results use (id, name, segment, city), never a profile, so no maskable field is persisted to disk.
 * The list is capped at ten and de-duplicated by customer id: re-opening a customer moves it to the
 * front rather than adding a duplicate.
 */

const KEY = 'c360.recently_viewed';
const MAX = 10;

function safeLocalStorage(): Storage | null {
  try {
    if (typeof localStorage === 'undefined') {
      return null;
    }
    const probe = '__c360_probe__';
    localStorage.setItem(probe, '1');
    localStorage.removeItem(probe);
    return localStorage;
  } catch {
    return null;
  }
}

const memory: { value: CustomerSearchHit[] } = { value: [] };

function isHit(value: unknown): value is CustomerSearchHit {
  return (
    typeof value === 'object' &&
    value !== null &&
    'customer_id' in value &&
    'customer_name' in value &&
    'customer_segment' in value
  );
}

export function readRecentlyViewed(): CustomerSearchHit[] {
  const storage = safeLocalStorage();
  if (storage === null) {
    return [...memory.value];
  }
  try {
    const raw = storage.getItem(KEY);
    if (raw === null) {
      return [];
    }
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(isHit).slice(0, MAX);
  } catch {
    return [];
  }
}

export function recordRecentlyViewed(hit: CustomerSearchHit): CustomerSearchHit[] {
  const current = readRecentlyViewed().filter((item) => item.customer_id !== hit.customer_id);
  const next = [hit, ...current].slice(0, MAX);

  const storage = safeLocalStorage();
  if (storage !== null) {
    try {
      storage.setItem(KEY, JSON.stringify(next));
    } catch {
      memory.value = next;
    }
  } else {
    memory.value = next;
  }
  return next;
}
