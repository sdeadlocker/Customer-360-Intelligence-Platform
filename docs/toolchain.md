# Toolchain

**Stack (locked):** React 18 + TypeScript / FastAPI + Python 3.12 · SQLite 3 · Amazon Bedrock ·
LangGraph · OpenTelemetry

## Prerequisites

| Tool   | Version   | Notes                                                        |
| ------ | --------- | ------------------------------------------------------------ |
| Python | 3.12.x    | Provisioned by `uv`; the system Python is not used or changed |
| uv     | ≥ 0.5     | Resolver and virtualenv manager                              |
| Node   | ≥ 20.19   | Required by Vite 8                                           |
| Docker | ≥ 24      | Optional; only for the local observability stack              |

Python 3.12 is a hard requirement, not a preference: `pyproject.toml` declares
`requires-python = ">=3.12,<3.13"`. If a different interpreter is the default, `uv` acquires the
right one rather than the environment being mutated:

```sh
uv python install 3.12
```

## Install

```sh
cd backend && uv sync --extra dev   # creates backend/.venv from uv.lock
cd ../frontend && npm ci            # installs from package-lock.json
```

Both sides pin exact versions and commit their lock file, so an install is reproducible rather than
"whatever resolved today".

## The gate

One command runs formatting, linting, typing and tests across both sides:

```sh
make check      # or, with no make available (default Windows):
npm run check
```

`npm run check` at the repository root is not a second implementation — it shells out to the same
tools, resolving the backend interpreter through `scripts/backend.mjs` so that
`.venv/bin` vs `.venv/Scripts` never appears in a script.

Individual legs:

| Command                | What it does                                             |
| ---------------------- | -------------------------------------------------------- |
| `npm run lint`         | ruff + black `--check`, ESLint                            |
| `npm run types`        | mypy (strict on `src/c360`), `tsc --noEmit`               |
| `npm run test`         | pytest with coverage ≥ 85%, Vitest                        |
| `npm run fmt`          | black, ruff `--fix`, Prettier `--write`                   |

Pytest treats warnings as errors. A third-party deprecation that this codebase cannot act on is
listed individually in `filterwarnings` rather than relaxing the default, so a new warning of our
own still fails the build.

## Running it

```sh
npm run dev:api   # http://127.0.0.1:8000  — /health, /ready, /docs
npm run dev:web   # http://127.0.0.1:5173  — proxies API paths to the backend
```

The ASGI target is a factory, not a module-level `app`:

```sh
uvicorn c360.main:create_app --factory --reload
```

Configuration is validated inside `create_app`. An import-time failure inside an ASGI server is
reported far less clearly than a factory call, so the trade is a `--factory` flag for a readable
error when a deployment is misconfigured.

## Configuration

Copy `backend/.env.example` to `backend/.env`. Every variable in design §18 is present with its
default; required-when-conditional variables are marked. No AWS credentials appear there — they come
from the default credential chain.

To run with no AWS account and no network calls:

```
LLM_PROVIDER=mock
```

With `LLM_PROVIDER=bedrock`, `BEDROCK_MODEL_ID` is required and startup fails without it.

## Observability

```sh
npm run obs:up     # Jaeger, OTel collector, Prometheus
npm run obs:down
```

Then point the API at the collector on the host and generate a request:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 npm run dev:api
curl -i http://127.0.0.1:8000/health
```

Jaeger UI is at http://localhost:16686 (service `c360-api`). Prometheus is at
http://localhost:9090.

The span attribute allowlist is enforced in-process at the span-processor level, not in the
collector. A collector-side filter would still let PII leave the process, and a collector redeployed
without that config would silently drop the protection. The collector's `attributes/scrub` processor
is defence in depth only.

Without Docker, the trace pipeline can still be exercised end to end:

```
OTEL_TRACES_EXPORTER=console
```

Spans print to stdout with the allowlist already applied, which is enough to confirm that no
disallowed attribute is present.
