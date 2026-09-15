/**
 * Type the `toHaveNoViolations` matcher onto vitest's `expect` (task 15.4).
 *
 * `jest-axe` ships a Jest matcher; `@types/jest-axe` declares it against Jest's `Matchers`, not
 * vitest's `Assertion`. This augments vitest's interface so `expect(container).toHaveNoViolations()`
 * is fully typed and the type-aware lint rules do not flag it as an unsafe untyped call.
 */
import 'vitest';

interface AxeMatchers<R = unknown> {
  toHaveNoViolations: () => R;
}

declare module 'vitest' {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  interface Assertion<T = unknown> extends AxeMatchers<T> {}
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  interface AsymmetricMatchersContaining extends AxeMatchers {}
}
