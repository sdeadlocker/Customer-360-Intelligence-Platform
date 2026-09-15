data "aws_caller_identity" "current" {}

locals {
  # Render {region}/{account_id} placeholders in the configured Bedrock ARNs.
  bedrock_model_arns = [
    for arn in var.bedrock_model_arns :
    replace(replace(arn, "{region}", var.region), "{account_id}", data.aws_caller_identity.current.account_id)
  ]
}

# ---------------------------------------------------------------- assume-role trust for ECS tasks
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ================================================================ execution role (Fargate agent)
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Let the execution role pull the secrets referenced by the task definition's `secrets` block.
data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid     = "InjectSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.jwt_private_key.arn,
      aws_secretsmanager_secret.telemetry_hash_salt.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.name_prefix}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# ================================================================ task role (the app at runtime)
resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  # Bedrock generation (agents / Q&A) + embeddings (RAG), scoped to the configured model ARNs.
  statement {
    sid = "BedrockInvoke"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.bedrock_model_arns
  }

  # Optional rerank over foundation models in-region.
  statement {
    sid       = "BedrockRerank"
    actions   = ["bedrock:Rerank"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/*"]
  }

  # Optional guardrail application.
  statement {
    sid       = "BedrockGuardrail"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = ["arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:guardrail/*"]
  }

  statement {
    sid     = "SecretsRead"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.jwt_private_key.arn,
      aws_secretsmanager_secret.telemetry_hash_salt.arn,
    ]
  }

  statement {
    sid = "ReportsBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.reports.arn,
      "${aws_s3_bucket.reports.arn}/*",
    ]
  }

  statement {
    sid = "EfsMount"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
      "elasticfilesystem:DescribeMountTargets",
    ]
    resources = [aws_efs_file_system.data.arn]
  }

  # Write-only telemetry to X-Ray and CloudWatch.
  statement {
    sid = "Telemetry"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "cloudwatch:PutMetricData",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name_prefix}-task-policy"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
