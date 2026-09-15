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
