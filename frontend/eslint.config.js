import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

/**
 * Type-aware linting is enabled via `projectService`, so rules that need to know whether a value
 * is a promise or a union actually work. That is the difference between ESLint catching a
 * forgotten `await` and not.
 */
export default tseslint.config(
  // `src/api/generated` is machine-generated from the OpenAPI spec (task 12.1); linting it would
  // fail on the generator's style and there is nothing to hand-fix there.
  { ignores: ['dist', 'coverage', 'node_modules', 'src/api/generated'] },

  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommendedTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
      // `configs.recommended` is still the legacy eslintrc shape; `configs.flat` holds the
      // flat-config objects this file needs.
      reactHooks.configs.flat.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },

  // The ESLint config itself and any other plain JS run under Node with no type information.
  {
    files: ['**/*.js'],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: globals.node,
    },
  },
);
