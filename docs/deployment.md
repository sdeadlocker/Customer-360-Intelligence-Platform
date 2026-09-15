# Deployment — path A (file-backed SQLite)

The platform ships as two container images plus the observability stack. This is **path A** from
Phase 18: the seeded read-only databases are baked into the API image, and the writable databases
(audit, checkpoints) live on a mounted volume. It is the fastest route to a running deployment and
matches the single-writer nature of the SQLite data layer; it is single-instance for the write path
(scale the read path with more tasks behind a load balancer).

## Images

| Image | Dockerfile | What it is |
|---|---|---|
| `c360-api` | `docker/Dockerfile.api` | FastAPI app. Bakes `customer.db` + `knowledge.db` (read-only) at build; writes `audit.db` + `checkpoints.db` under `/data` (a mounted volume). |
| `c360-web` | `docker/Dockerfile.web` | Vite SPA served by nginx, reverse-proxying the API paths to the api service (same-origin, SSE-friendly). |

## Prerequisites

- Docker with BuildKit (`DOCKER_BUILDKIT=1`, default on modern Docker).
- For **real Bedrock at runtime**: AWS credentials available to the api container (a task role on
  AWS; locally, an env-var trio or mounted `~/.aws`). Model access enabled for the chosen model.

## Local: build and run the whole app

```bash
# From the repo root. Builds both images and runs them together (path A).
docker compose -f docker/docker-compose.app.yml up --build
#   web:  http://localhost:8080        api (direct): http://localhost:8000
```

Sign in with a seeded role (e.g. Contact Center) and use the search-landing Ask AI panel.

### LLM provider

By default the stack runs `LLM_PROVIDER=mock` (deterministic, no AWS). To use **real Claude
(Haiku 4.5) + RAG**, set these before `up` (a `.env` beside the compose file, or the shell):

```bash
LLM_PROVIDER=bedrock
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
BEDROCK_EMBED_MODEL_ID=amazon.titan-embed-text-v2:0
# Credentials for the api container (local only; on AWS use a task role and omit these):
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...      # if using temporary/SSO credentials
```

Swap `BEDROCK_MODEL_ID` to `us.anthropic.claude-sonnet-4-5-20250929-v1:0` once Sonnet model access
is enabled.

### Embeddings baked into the image (RAG quality)

The knowledge index is baked at build time. Two choices:

- **`EMBED_PROVIDER=mock` (default):** hermetic, offline build. RAG retrieval uses pseudo-embeddings
  — fine for wiring/demo of the flow, but retrieval relevance is weak. Generation can still be real
  Bedrock at runtime.
- **`EMBED_PROVIDER=bedrock`:** bakes real Titan v2 embeddings, so RAG retrieval is meaningful. The
  build makes Bedrock embedding calls, so it needs credentials as a BuildKit secret:

  ```bash
  docker build -f docker/Dockerfile.api \
    --build-arg EMBED_PROVIDER=bedrock \
    --secret id=aws,src=$HOME/.aws/credentials \
    -t c360-api:bedrock .
  ```

  (The api Dockerfile's ingest step reads the secret for that one `RUN`; see the comment there.)

## Notes carried from the design

- **JWT signing key:** unset means an ephemeral in-process RSA keypair (tokens die on restart —
  correct for demo). For real use, mount a PEM and set `JWT_PRIVATE_KEY_PATH`, or switch to OIDC.
- **Telemetry:** `OTEL_ENABLED=false` by default in the app compose. Point
  `OTEL_EXPORTER_OTLP_ENDPOINT` at the collector (the observability stack in
  `docker/docker-compose.yml`) to export traces/metrics.
- **Single writer:** path A runs one api task for the write path (audit DB is single-writer). See
  Phase 18 in the tasks plan for the managed-datastore path (B) if HA/scale is required.

## AWS deployment (path A) — Phase 20

Infrastructure-as-code lives in [`deploy/`](../deploy). The compute decision is **ECS on Fargate**
(load balancer, per-service autoscaling, first-class task roles for Bedrock) over App Runner/EKS,
and the datastore is **path A** (read-only DBs baked into the api image; writable audit/checkpoint
DBs on EFS).

```
Internet
   │  HTTPS (ACM)
   ▼
Application Load Balancer ── /            → web target group  (nginx SPA, :8080)
                          └─ /api,/ask,…  → api target group  (FastAPI,   :8000)
   │
   ├── ECS Fargate service: web  (2+ tasks, autoscaled on CPU)   [private subnets]
   └── ECS Fargate service: api  (1 task — SQLite single-writer) [private subnets]
          ├── task role → Bedrock InvokeModel/Stream (agents + RAG), Secrets, S3, EFS, X-Ray/CW
          ├── EFS access point → /data  (audit.db, checkpoints.db, signals.db)
          └── ADOT sidecar → X-Ray (traces) + CloudWatch (metrics/logs)
```

### Bedrock for the LLM (agents + RAG)

The api uses Bedrock two ways, both through the task role (no keys in the image or env):

- **Generation / agents** — `ChatBedrockConverse` calls `bedrock:InvokeModel` and
  `bedrock:InvokeModelWithResponseStream` for the Q&A ReAct loop. Model set by `BEDROCK_MODEL_ID`
  (default the Claude Haiku 4.5 `us.` cross-region inference profile; swap to Sonnet 4.5 once model
  access is enabled).
- **RAG embeddings** — Titan (`amazon.titan-embed-text-v2:0`) via the same actions, for retrieval at
  query time and, optionally, for baking the knowledge index at image build.

Claude 4.5 is only reachable through a **cross-region inference profile**, so the invoke permission
must cover the underlying regional foundation-model ARNs (`us-east-1`, `us-east-2`, `us-west-2`), not
just the profile ARN. The task policy in `deploy/terraform/iam.tf` and
[`deploy/iam/task-role-policy.json`](../deploy/iam/task-role-policy.json) already list these.

**Prerequisite:** enable model access for the chosen models in the deployment region from the Bedrock
console (Model access), or every invoke returns `AccessDeniedException`.

### IAM: two identities, kept apart

The account's broad AWS-managed `FullAccess` policies (see the console list captured in
[`deploy/iam/deployer-managed-policies.json`](../deploy/iam/deployer-managed-policies.json)) belong on
the **deployer/CI identity** that runs Terraform and pushes images. The **running app** uses a
separate **least-privilege task role** (Bedrock invoke on specific model ARNs, `GetSecretValue` on
`c360/*`, the reports bucket, the one EFS file system, and write-only telemetry). This satisfies
`tasks.md` 20.5 and keeps full-access off the runtime.

> **Confirm before deploying:** the shared policy list did **not** include an **ECR** policy. Pushing
> images needs `AmazonEC2ContainerRegistryFullAccess` (or `...PowerUser`). ACM access was also not
> visible — needed only if Terraform provisions the TLS certificate. See `deploy/iam/README.md`.

### Runbook

Prerequisites: Terraform ≥ 1.6, Docker with BuildKit, the AWS CLI authenticated as the deployer
identity, and Bedrock model access enabled in the region.

```bash
# 1. Provision the registry first so images have somewhere to go.
terraform -chdir=deploy/terraform init
terraform -chdir=deploy/terraform apply -target=aws_ecr_repository.api -target=aws_ecr_repository.web

# 2. Build + push both images, tagged by git SHA.
#    EMBED_PROVIDER=bedrock bakes real Titan embeddings (better RAG); mock is offline/hermetic.
REGION=us-east-1 ./deploy/scripts/build-and-push.sh          # bash / CI
#    or on Windows:
#    ./deploy/scripts/build-and-push.ps1 -Region us-east-1

# 3. Provision the rest and deploy the pushed tag (copy terraform.tfvars.example first).
terraform -chdir=deploy/terraform apply \
  -var=api_image_tag=<git-sha> -var=web_image_tag=<git-sha>

# 4. Populate the placeholder secrets (JWT signing key, telemetry salt), then roll the service.
REGION=us-east-1 ./deploy/scripts/set-secrets.sh

# 5. For HTTPS: if Terraform created the ACM cert, add the DNS validation records from the
#    `acm_certificate_validation_records` output, then point your domain at `alb_dns_name`.
```

The app URL is the `app_url` Terraform output.

### Post-deploy verification (tasks.md 20.9)

- Hit `app_url`, sign in across roles, and confirm entitlement-scoped masked data renders.
- Ask a question from the search landing and confirm a **real Bedrock answer streams** end to end.
- In X-Ray, confirm a trace appears for the ask with **no** disallowed attributes (no PII, monetary
  values, prompts or completions — `OTEL_CAPTURE_PROMPT_CONTENT` stays `false`).
- Optionally run the Phase 16.6 Playwright journeys against `app_url`.

### Rollback

Images are immutable and tagged by git SHA. Roll back by re-applying with the previous tag:

```bash
terraform -chdir=deploy/terraform apply -var=api_image_tag=<prev-sha> -var=web_image_tag=<prev-sha>
```

### Operational notes

- **Single api writer.** Path A keeps `api_desired_count = 1` because the audit DB is single-writer;
  the deploy uses min-healthy 0 / max 100 so a new task never overlaps the old writer. Scale the read
  path with more tasks only if you accept the constraint, or move to **path B** (RDS/OpenSearch) for
  true HA.
- **EFS.** The writable `/data` volume is an EFS access point pinned to the container's non-root
  uid/gid (1000). Audit and conversation state survive a task replace.
- **Cost drivers.** Fargate tasks (api + web), two NAT gateways, the ALB, EFS, and Bedrock token
  usage. NAT is the usual surprise on a low-traffic deployment; a single NAT (set `az_count = 1`) or
  VPC endpoints for Bedrock/ECR/Secrets cut it, at the cost of AZ resilience.
