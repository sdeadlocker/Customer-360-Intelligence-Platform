# IAM for Customer 360 deployment (Phase 20)

Two identities, deliberately kept apart. The account's broad `FullAccess` managed policies (the
ones shown in the console list) belong on the **deployer**, never on the running app.

## 1. Deployer / CI identity — broad, provisions infrastructure

The identity a human or CI pipeline uses to run Terraform and push images. It carries the
AWS-managed policies inventoried in [`deployer-managed-policies.json`](./deployer-managed-policies.json).
That set is fine here because provisioning genuinely touches ECR, ECS, VPC, ELB, EFS, IAM,
CloudWatch, Secrets Manager and (for path B) RDS/OpenSearch.

**Gap to confirm:** the shared policy list did not include an **ECR** policy
(`AmazonEC2ContainerRegistryFullAccess` or `...PowerUser`). Pushing the `c360-api` / `c360-web`
images needs it. Confirm it is attached before running `deploy/scripts/build-and-push`.

## 2. ECS task role — least-privilege, what the app runs under at runtime

Defined in [`task-role-policy.json`](./task-role-policy.json). This is the role the API container
assumes. It is scoped tightly per `tasks.md` 20.5:

| Capability | Actions | Scope |
|---|---|---|
| LLM generation (agents / Q&A) | `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` | Claude Haiku/Sonnet model + inference-profile ARNs |
| RAG embeddings | same two actions | Titan `amazon.titan-embed-text-v2:0` ARN |
| Rerank (optional) | `bedrock:Rerank` | foundation models in region |
| Guardrail (optional) | `bedrock:ApplyGuardrail` | account guardrails |
| Runtime secrets | `secretsmanager:GetSecretValue` | `c360/*` secret ARNs only |
| Reports / assets | `s3:GetObject/PutObject/ListBucket` | the reports bucket only |
| Writable data plane | `elasticfilesystem:ClientMount/ClientWrite` | the one EFS file system |
| Telemetry | X-Ray + CloudWatch put | `*` (write-only telemetry) |

The cross-region statement covers the `us.` inference profiles (Claude 4.5 is only reachable via a
cross-region inference profile, which fans out to `us-east-1/us-east-2/us-west-2` foundation-model
ARNs — the invoke permission must be granted on those regional targets, not just the profile ARN).

## 3. ECS task execution role — pulls images, ships logs, injects secrets

Defined in [`execution-role-policy.json`](./execution-role-policy.json). Attach alongside the AWS
managed `AmazonECSTaskExecutionRolePolicy`. This is what Fargate itself uses to start the task; it
is separate from the task role above.

## Placeholders

The JSON files use `${region}`, `${account_id}`, `${reports_bucket}` and `${efs_id}`. Terraform in
`../terraform` renders these from its own variables/resources; if you apply the policies by hand,
substitute the real values first.

## Credentials never live in source

No access keys anywhere. Locally the SDK uses the default chain (SSO profile / env); on AWS the task
role supplies credentials automatically. This matches design §18 and `tasks.md` 20.6.
