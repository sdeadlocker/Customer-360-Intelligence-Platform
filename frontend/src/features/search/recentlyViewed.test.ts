import { beforeEach, describe, expect, it } from 'vitest';

import type { CustomerSearchHit } from '../../api/types';
import { readRecentlyViewed, recordRecentlyViewed } from './recentlyViewed';

function hit(id: string): CustomerSearchHit {
  return {
    customer_id: id,
    customer_name: `Name ${id}`,
    customer_segment: 'MASS',
    city: 'Austin',
  };
}

describe('recentlyViewed', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('starts empty', () => {
    expect(readRecentlyViewed()).toEqual([]);
  });

  it('records most-recent first', () => {
    recordRecentlyViewed(hit('CUST-1'));
    const list = recordRecentlyViewed(hit('CUST-2'));
    expect(list.map((h) => h.customer_id)).toEqual(['CUST-2', 'CUST-1']);
  });

  it('de-duplicates and moves a re-viewed customer to the front', () => {
    recordRecentlyViewed(hit('CUST-1'));
    recordRecentlyViewed(hit('CUST-2'));
    const list = recordRecentlyViewed(hit('CUST-1'));
    expect(list.map((h) => h.customer_id)).toEqual(['CUST-1', 'CUST-2']);
  });

  it('caps the list at ten', () => {
    for (let i = 0; i < 15; i += 1) {
      recordRecentlyViewed(hit(`CUST-${i}`));
    }
    const list = readRecentlyViewed();
    expect(list).toHaveLength(10);
    // Newest kept, oldest dropped.
    expect(list[0]?.customer_id).toBe('CUST-14');
    expect(list.some((h) => h.customer_id === 'CUST-0')).toBe(false);
  });

  it('ignores a corrupt stored value', () => {
    localStorage.setItem('c360.recently_viewed', '{not json');
    expect(readRecentlyViewed()).toEqual([]);
  });
});
