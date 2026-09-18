import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
});

// Register the axe-core accessibility matcher globally so every test can assert
// `expect(container).toHaveNoViolations()` (task 15.4). The matcher's types are declared in
// `src/test/jest-axe.d.ts`.
import { toHaveNoViolations } from 'jest-axe';
import { expect } from 'vitest';

expect.extend(toHaveNoViolations);

// jsdom implements no scrolling, so `Element.prototype.scrollIntoView` is simply absent. Any
// component that scrolls a section into view after a nav click (the dashboard rail and the landing
// rail both do) would otherwise throw inside a `requestAnimationFrame` callback, surfacing as an
// unhandled error unrelated to what the test is asserting. Stub it as a no-op: scrolling has no
// observable meaning in a zero-height virtual viewport.
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollIntoView !== 'function') {
  Element.prototype.scrollIntoView = function scrollIntoView(): void {
    /* no-op in jsdom */
  };
}
