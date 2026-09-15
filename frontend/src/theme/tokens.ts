/**
 * The colour tokens, mirrored from `tokens.css`, plus the text/surface pairings that carry meaning
 * (task 12.2). The contrast audit test reads `CONTRAST_PAIRS` and asserts each clears its WCAG 2.1
 * AA threshold, so the assertion is against the real token values rather than a hand-copied table.
 *
 * Only the pairings a user actually reads or relies on are listed. A colour that never sits under
 * text (a soft pill fill, say) has no text-contrast requirement and is deliberately absent.
 */

/** WCAG 2.1 AA minimums. */
export const AA_BODY = 4.5;
export const AA_LARGE = 3; // large text and non-text UI (borders, focus rings, gauges)

/** One palette's resolved hex values for the semantic colour tokens (must match `tokens.css`). */
export interface Palette {
  readonly bg: string;
  readonly surface: string;
  readonly surfaceRaised: string;
  readonly border: string;
  readonly borderStrong: string;
  readonly borderInteractive: string;
  readonly text: string;
  readonly textMuted: string;
  readonly textSubtle: string;
  readonly accent: string;
  readonly accentStrong: string;
  readonly onAccent: string;
  readonly pass: string;
  readonly warn: string;
  readonly fail: string;
}

/** Light palette (the default `:root` theme) — professional blue/indigo accent. */
export const LIGHT_COLORS: Palette = {
  bg: '#f4f6fb',
  surface: '#ffffff',
  surfaceRaised: '#eef1f8',
  border: '#dfe3ec',
  borderStrong: '#c3c9d6',
  borderInteractive: '#5f6b85',
  text: '#161c2d',
  textMuted: '#454f63',
  textSubtle: '#586074',
  accent: '#4338ca',
  accentStrong: '#4f46e5',
  onAccent: '#ffffff',
  pass: '#137a3f',
  warn: '#8a5300',
  fail: '#c0202a',
} as const;

/** Dark palette (applied under `data-theme="dark"` or a dark OS preference). */
export const DARK_COLORS: Palette = {
  bg: '#0f1117',
  surface: '#181c26',
  surfaceRaised: '#1f2430',
  border: '#333b4d',
  borderStrong: '#48536b',
  borderInteractive: '#7a869f',
  text: '#e6e9f0',
  textMuted: '#a7b0c4',
  textSubtle: '#8b95ab',
  accent: '#8da2ff',
  accentStrong: '#4f46e5',
  onAccent: '#ffffff',
  pass: '#59d17d',
  warn: '#f0c548',
  fail: '#ff8a8a',
} as const;

/** Back-compat alias: the dark palette was the original single `COLORS` table. */
export const COLORS = DARK_COLORS;

export interface ContrastPair {
  readonly name: string;
  readonly fg: string;
  readonly bg: string;
  /** The threshold this pairing must clear. Body text needs 4.5:1; large text / non-text needs 3:1. */
  readonly min: number;
}

/** Every foreground/background pairing the UI depends on, for one palette. */
function pairsFor(theme: string, c: Palette): readonly ContrastPair[] {
  return [
    { name: `${theme}: text on surface`, fg: c.text, bg: c.surface, min: AA_BODY },
    { name: `${theme}: text on bg`, fg: c.text, bg: c.bg, min: AA_BODY },
    { name: `${theme}: muted text on surface`, fg: c.textMuted, bg: c.surface, min: AA_BODY },
    { name: `${theme}: muted text on bg`, fg: c.textMuted, bg: c.bg, min: AA_BODY },
    { name: `${theme}: subtle text on surface`, fg: c.textSubtle, bg: c.surface, min: AA_BODY },
    { name: `${theme}: accent link on surface`, fg: c.accent, bg: c.surface, min: AA_BODY },
    { name: `${theme}: accent link on bg`, fg: c.accent, bg: c.bg, min: AA_BODY },
    { name: `${theme}: text on accent fill`, fg: c.onAccent, bg: c.accentStrong, min: AA_BODY },
    { name: `${theme}: pass text on surface`, fg: c.pass, bg: c.surface, min: AA_BODY },
    { name: `${theme}: warn text on surface`, fg: c.warn, bg: c.surface, min: AA_BODY },
    { name: `${theme}: fail text on surface`, fg: c.fail, bg: c.surface, min: AA_BODY },
    // Non-text UI that conveys a control boundary needs 3:1 (requirement 16.1). A purely decorative
    // separator (`--color-border`) has no WCAG minimum and is deliberately not asserted; the
    // interactive boundary and the focus ring, which a user relies on, are.
    {
      name: `${theme}: interactive border on surface`,
      fg: c.borderInteractive,
      bg: c.surface,
      min: AA_LARGE,
    },
    {
      name: `${theme}: interactive border on bg`,
      fg: c.borderInteractive,
      bg: c.bg,
      min: AA_LARGE,
    },
    { name: `${theme}: focus ring on surface`, fg: c.accent, bg: c.surface, min: AA_LARGE },
    { name: `${theme}: focus ring on bg`, fg: c.accent, bg: c.bg, min: AA_LARGE },
  ];
}

/**
 * Every meaning-carrying pairing, in **both** themes. Text tones are checked against both the app
 * background and the card surface, because a token used on both must be readable on both. The audit
 * test asserts each clears its AA threshold, so a palette edit in either theme fails the build.
 */
export const CONTRAST_PAIRS: readonly ContrastPair[] = [
  ...pairsFor('light', LIGHT_COLORS),
  ...pairsFor('dark', DARK_COLORS),
];

/** Parse a `#rrggbb` string into linearised sRGB channels. */
function channels(hex: string): readonly [number, number, number] {
  const value = hex.replace('#', '');
  const r = parseInt(value.slice(0, 2), 16) / 255;
  const g = parseInt(value.slice(2, 4), 16) / 255;
  const b = parseInt(value.slice(4, 6), 16) / 255;
  return [r, g, b];
}

/** sRGB → linear, per the WCAG relative-luminance definition. */
function linearize(component: number): number {
  return component <= 0.03928 ? component / 12.92 : ((component + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance of a `#rrggbb` colour. */
export function relativeLuminance(hex: string): number {
  const [r, g, b] = channels(hex).map(linearize) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio between two `#rrggbb` colours, in the range 1..21. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const lighter = Math.max(la, lb);
  const darker = Math.min(la, lb);
  return (lighter + 0.05) / (darker + 0.05);
}
