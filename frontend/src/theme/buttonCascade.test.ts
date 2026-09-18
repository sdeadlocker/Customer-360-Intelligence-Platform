import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * Cascade guard for primary buttons.
 *
 * `.button--primary` carries the brand gradient plus white `--color-on-accent` text. A later generic
 * `.button` rule that sets a `background` at the same specificity silently overwrites the gradient
 * while leaving the white text in place — a white label on a white button, invisible until a hover
 * rule happens to recolour it. That shipped once and was only caught by eye, because no unit test
 * observes the cascade and axe cannot judge a colour that only resolves at paint time.
 *
 * So the invariant is asserted statically instead: **any unscoped `.button` rule that declares a
 * background must exclude `.button--primary`.** A scoped rule (`.ask-panel__row .button--primary`,
 * `.landing__ask .button`) is out of scope — those raise specificity deliberately.
 */

const CSS_PATH = resolve(process.cwd(), 'src/index.css');

interface CssRule {
  readonly selector: string;
  readonly body: string;
  /** Character offset in the stylesheet — the tie-breaker when specificity is equal. */
  readonly at: number;
}

/**
 * Split the stylesheet into `selector { body }` pairs.
 *
 * Deliberately naive: nested at-rules yield the at-rule text glued onto their first inner selector,
 * which is harmless here because every selector this test cares about is matched by substring.
 */
function parseRules(css: string): readonly CssRule[] {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const out: CssRule[] = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match: RegExpExecArray | null = pattern.exec(withoutComments);
  while (match !== null) {
    out.push({ selector: (match[1] ?? '').trim(), body: match[2] ?? '', at: match.index });
    match = pattern.exec(withoutComments);
  }
  return out;
}

/** Is this compound selector plain `.button`, optionally with pseudo-classes, and nothing else? */
function isUnscopedButton(compound: string): boolean {
  return /^\.button(?::[a-z-]+(?:\([^)]*\))?)*$/.test(compound.trim());
}

function declaresBackground(body: string): boolean {
  return /(^|[;{\s])background(-color|-image)?\s*:/.test(body);
}

describe('index.css — primary button cascade', () => {
  const rules = parseRules(readFileSync(CSS_PATH, 'utf8'));

  it('parses the stylesheet', () => {
    expect(rules.length).toBeGreaterThan(100);
  });

  it('never lets an unscoped .button background rule swallow .button--primary', () => {
    const primaryBackgroundAt = rules
      .filter((rule) => rule.selector.split(',').some((c) => c.trim() === '.button--primary'))
      .filter((rule) => declaresBackground(rule.body))
      .reduce((latest, rule) => Math.max(latest, rule.at), -1);
    expect(primaryBackgroundAt).toBeGreaterThan(-1);

    // Only a rule positioned *after* the primary background can overwrite it at equal specificity;
    // the base `.button { background: transparent }` earlier in the file is harmless.
    const offenders = rules
      .filter((rule) => rule.at > primaryBackgroundAt)
      .filter((rule) => declaresBackground(rule.body))
      .filter((rule) => rule.selector.split(',').some(isUnscopedButton))
      .filter((rule) => !rule.selector.includes(':not(.button--primary)'))
      .map((rule) => rule.selector);

    // Expected to be empty: the generic surface rule is written `.button:not(.button--primary)`.
    expect(offenders).toEqual([]);
  });

  it('keeps the brand gradient on .button--primary', () => {
    const primaryBackgrounds = rules
      .filter((rule) => rule.selector.split(',').some((c) => c.trim() === '.button--primary'))
      .filter((rule) => declaresBackground(rule.body));

    expect(primaryBackgrounds.length).toBeGreaterThan(0);
    // The last background declaration on the plain primary selector is the one that paints.
    const last = primaryBackgrounds[primaryBackgrounds.length - 1];
    expect(last?.body).toContain('--gradient-accent');
  });

  it('does not recolour a primary label on hover', () => {
    // Accent-indigo text over the accent gradient would fail contrast, so the hover recolour must
    // exclude primary just as the background rule does.
    const hoverRecolours = rules
      .filter((rule) => /(^|,)\s*\.button:hover/.test(rule.selector))
      .filter((rule) => /(^|[;{\s])color\s*:/.test(rule.body));

    for (const rule of hoverRecolours) {
      expect(rule.selector).toContain(':not(.button--primary)');
    }
  });
});
