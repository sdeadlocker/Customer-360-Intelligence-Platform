import { describe, expect, it } from 'vitest';

import { CONTRAST_PAIRS, contrastRatio, relativeLuminance } from './tokens';

/**
 * The contrast audit (task 12.2, requirement 16.1). Every meaningful colour pairing must clear its
 * WCAG 2.1 AA threshold; a palette change that drops a pairing below the ratio fails here rather
 * than shipping unreadable text.
 */
describe('design token contrast', () => {
  it.each(CONTRAST_PAIRS)('$name clears its AA threshold', (pair) => {
    const ratio = contrastRatio(pair.fg, pair.bg);
    expect(ratio).toBeGreaterThanOrEqual(pair.min);
  });

  it('computes a known contrast ratio correctly (black on white is 21:1)', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 1);
  });

  it('is symmetric in its arguments', () => {
    expect(contrastRatio('#0f1117', '#e6e9f0')).toBeCloseTo(contrastRatio('#e6e9f0', '#0f1117'), 5);
  });

  it('luminance of white is 1 and black is 0', () => {
    expect(relativeLuminance('#ffffff')).toBeCloseTo(1, 5);
    expect(relativeLuminance('#000000')).toBeCloseTo(0, 5);
  });
});
