# Customer 360 Intelligence Platform

An AI-augmented, single-pane-of-glass view of a banking customer. It unifies profile, financial,
credit, risk, behavioral, relationship, asset and life-event data into one governed dashboard, then
layers grounded AI narratives, natural-language Q&A, and a hybrid RAG knowledge layer on top —
with entitlements, field masking, citations and full observability enforced end to end.

## Problem

A banker serving a customer today stitches the picture together from many systems (core banking, CRM,
credit, risk, marketing, document stores). It's slow, error-prone, and inconsistent across roles.
Bolting generative AI onto that data adds two new risks: hallucinated figures shown as fact, and
uncontrolled exposure of sensitive data to models and telemetry.

This platform removes the swivel-chair and makes AI safe for a regulated bank:

- **Deterministic before generative** — net worth, utilization and aggregates are computed in SQL /
  integer arithmetic. The LLM narrates; it never calculates.
- **Every AI figure is validated against a source record before it is shown.** If a claim can't be
  traced to a fact within tolerance, it's rejected.
- **Masking at the serialization boundary** and **entitlements enforced inside queries**, with every
  read, agent run and retrieval written to an append-only audit log.

## Use cases

The platform serves six roles, each seeing only what their role and entitlement allow:

| Persona | Primary need |
|---|---|
| Relationship Manager | Full 360 view, next best actions |
| Wealth Advisor | Investments, net worth, household |
| Contact Center Agent | Identity verification, recent activity (masked financial detail) |
| Branch Employee | Profile, holdings, basic servicing (masked) |
| Risk Analyst | Risk, fraud, delinquency, exposure |
| Marketing Analyst | Segments, offers, propensity (aggregate / pseudonymized) |

Headline capabilities: universal customer search, a 360 dashboard (Cockpit / Spotlight / Compact
views), seven grounded AI agents (Summary, Financial Health, Risk, Life Event, Offer, Relationship,
Journey), natural-language Q&A, hybrid RAG over an institutional knowledge corpus, a proactive
signals worklist, and scheduled / branded PDF reports.

## Tech stack

- **Frontend:** React 18 + TypeScript, Vite 8
- **Backend:** FastAPI + Python 3.12 (ASGI factory), `uv` for dependency management
- **Data:** SQLite 3 (WAL, JSON1, FTS5 for lexical search, `sqlite-vec` for vectors) — read-only at
  runtime
- **AI:** Amazon Bedrock (Converse API + Titan Embeddings V2), LangGraph orchestration, hybrid RAG
  with Reciprocal Rank Fusion and optional Bedrock rerank
- **Observability:** OpenTelemetry (GenAI semantic conventions) → OTel collector → Prometheus,
  Grafana, Jaeger locally, or CloudWatch / X-Ray on AWS
- **Deploy:** Docker, AWS (ECS/EC2), Terraform (`deploy/terraform`)

## Architecture

Layered so each guarantee (masking, grounding, entitlement, observability) lives in exactly one place:

```
Presentation (React + TS)
  App shell · Login · Search landing (Q&A + Signals) · 360 Dashboard · Reports · RUM
        │
Edge (FastAPI)
  CORS · correlation/trace ID · AuthN/AuthZ · mask filter · audit sink
        │
Application services
  Customer · Financial · Relationship · Risk · Offer · Journey  →  C360 Aggregator
        │
Agentic layer (LangGraph)
  Dashboard StateGraph · Q&A ReAct graph · typed Tool Registry · Claim Validator · LLMProvider→Bedrock
        │
Knowledge layer (RAG)                         Data layer
  Hybrid Retriever · RRF + rerank ·           Repository ports ·
  Titan embeddings · knowledge.db             customer.db (read-only) · audit.db (append-only) ·
  (vec0 + FTS5)                               signals.db / reports.db (writable)
```

Non-negotiable rules: agents never touch the database (they read through the same typed tool registry
the REST API uses); retrieval is for knowledge, never customer facts; the graph is derived and
rebuildable; telemetry carries no customer content; and every AI change is gated by an evaluation
suite in CI.

## Flow

1. A user signs in with a role and searches for a customer (FTS5 typeahead, entitlement applied
   inside the query).
2. The `C360Aggregator` fans out concurrently over read-only connections and returns the deterministic
   360 payload (3 s p95 budget), each widget running a loading → ready / partial / restricted / error
   state machine.
3. AI cards and Q&A stream in separately (5 s p95 budget) via LangGraph: agents call typed tools,
   deterministic aggregation runs in SQL, and the Claim Validator rejects any figure it can't cite.
4. Masking is applied at serialization; every read, agent run and retrieval is audited; OpenTelemetry
   traces the whole path with per-agent token, cost and latency metrics.

## How to replicate

**Prerequisites:** Python 3.12.x, `uv` ≥ 0.5, Node ≥ 20.19, Docker ≥ 24 (optional, for local
observability).

```sh
# 1. Clone
git clone https://github.com/sdeadlocker/Customer-360-Intelligence-Platform.git
cd Customer-360-Intelligence-Platform

# 2. Install dependencies (reproducible from lock files)
cd backend && uv sync --extra dev      # creates backend/.venv from uv.lock
cd ../frontend && npm ci               # installs from package-lock.json
cd ..

# 3. Configure the backend
#    Copy the example env and (optionally) set the LLM provider.
#    Use LLM_PROVIDER=mock to run with no AWS account and no network calls.
cp backend/.env.example backend/.env

# 4. Build the local databases (databases are generated, never committed)
cd backend
.venv/Scripts/python -m c360 seed             # synthetic customer database   (Windows)
.venv/Scripts/python -m c360 recompute        # derived values, search index, graph
.venv/Scripts/python -m c360 ingest-knowledge # knowledge.db from the corpus
.venv/Scripts/python -m c360 detect-signals   # proactive signals worklist
cd ..
# On macOS/Linux use .venv/bin/python instead of .venv/Scripts/python
```

**Run it** (two terminals):

```sh
npm run dev:api    # http://127.0.0.1:8000  — /health, /ready, /docs
npm run dev:web    # http://127.0.0.1:5173  — proxies API paths to the backend
```

**Quality gate** — formatting, linting, typing and tests across both sides:

```sh
make check         # or, on Windows without make:
npm run check
```

**Local observability stack** (optional):

```sh
npm run obs:up     # Jaeger (:16686), OTel collector, Prometheus (:9090)
npm run obs:down
```

**Evaluation gate** (deterministic, mock provider):

```sh
cd backend && .venv/Scripts/python -m c360 eval run --mode ci
```

> Note: generated SQLite databases (`*.db`), virtualenvs, `node_modules`, build output, Terraform
> providers/state and secrets are intentionally git-ignored to keep the repository small. They are
> rebuilt locally with the steps above.
