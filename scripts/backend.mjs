#!/usr/bin/env node
/**
 * Run a backend tool through the pinned virtualenv interpreter.
 *
 * `make` is not available on a default Windows install, so the repository-root npm scripts are the
 * portable entry point. Resolving the interpreter here rather than in package.json keeps the
 * scripts free of `.venv/bin` vs `.venv/Scripts` branching.
 *
 *   node scripts/backend.mjs -m pytest
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const backendDir = join(repoRoot, 'backend');

const candidates =
  process.platform === 'win32'
    ? [join(backendDir, '.venv', 'Scripts', 'python.exe')]
    : [join(backendDir, '.venv', 'bin', 'python3'), join(backendDir, '.venv', 'bin', 'python')];

const python = candidates.find((candidate) => existsSync(candidate));

if (python === undefined) {
  process.stderr.write(
    `No backend virtualenv found. Expected one of:\n  ${candidates.join('\n  ')}\n` +
      `Create it with:  cd backend && uv sync --extra dev\n`,
  );
  process.exit(1);
}

const args = process.argv.slice(2);
if (args.length === 0) {
  process.stderr.write('Usage: node scripts/backend.mjs <python args...>\n');
  process.exit(2);
}

// `cwd` is the backend package so pyproject.toml's tool configuration and relative paths resolve.
const result = spawnSync(python, args, { cwd: backendDir, stdio: 'inherit' });

if (result.error) {
  process.stderr.write(`Failed to run ${python}: ${result.error.message}\n`);
  process.exit(1);
}

process.exit(result.status ?? 1);
