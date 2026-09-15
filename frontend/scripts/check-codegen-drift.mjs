// Fail the build when the committed API types have drifted from the OpenAPI spec (task 12.1).
//
// `npm run codegen:check` regenerates the types from `openapi.json` into a throwaway file and this
// script diffs it against the committed `src/api/generated/schema.ts`. Any difference means someone
// changed the spec (or the generator) without re-running codegen, and the generated client no longer
// matches the contract the backend publishes — exactly the drift task 12.1 requires to be a build
// failure. The throwaway file is always removed so a failed check leaves no untracked artifact.

import { readFileSync, rmSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const committed = join(here, '..', 'src', 'api', 'generated', 'schema.ts');
const generated = join(here, '..', 'src', 'api', 'generated', 'schema.check.ts');

function cleanup() {
  if (existsSync(generated)) {
    rmSync(generated);
  }
}

try {
  if (!existsSync(committed)) {
    console.error(
      'API types are missing. Run `npm run codegen` to generate src/api/generated/schema.ts.',
    );
    process.exit(1);
  }

  const committedText = readFileSync(committed, 'utf8');
  const generatedText = readFileSync(generated, 'utf8');

  if (committedText !== generatedText) {
    console.error(
      'API types are out of date with openapi.json.\n' +
        'The generated client has drifted from the published OpenAPI contract.\n' +
        'Re-export the spec from the backend and run `npm run codegen`, then commit the result.',
    );
    process.exit(1);
  }

  console.log('API types are in sync with openapi.json.');
} finally {
  cleanup();
}
