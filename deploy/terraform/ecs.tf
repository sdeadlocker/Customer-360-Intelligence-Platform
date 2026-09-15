resource "aws_ecs_cluster" "main" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------------------------------------------------------------- log groups
resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.name_prefix}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${var.name_prefix}-web"
  retention_in_days = var.log_retention_days
}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  api_image    = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
  web_image    = "${aws_ecr_repository.web.repository_url}:${var.web_image_tag}"
  adot_image   = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
  otel_enabled = var.otel_enabled

  # The api container's environment. Bedrock wiring for BOTH agents (generation) and RAG (embeddings)
  # flows from here; credentials come from the task role, never from env, per design §18 / tasks 20.5.
  api_environment = [
    { name = "ENVIRONMENT", value = var.environment },
    { name = "API_HOST", value = "0.0.0.0" },
    { name = "API_PORT", value = "8000" },
    { name = "LOG_FORMAT", value = "json" },

    # LLM / Bedrock
    { name = "LLM_PROVIDER", value = var.llm_provider },
    { name = "AWS_REGION", value = var.region },
    { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
    { name = "BEDROCK_EMBED_MODEL_ID", value = var.bedrock_embed_model_id },
    { name = "BEDROCK_EMBED_DIMENSIONS", value = tostring(var.bedrock_embed_dimensions) },
    { name = "BEDROCK_GUARDRAIL_ID", value = var.bedrock_guardrail_id },

    # RAG retrieval on (design §17)
    { name = "RETRIEVAL_ENABLED", value = "true" },

    # Writable data plane on the EFS mount; read-only DBs are baked into the image (path A).
    { name = "SQLITE_AUDIT_DB_PATH", value = "/data/audit.db" },
    { name = "SQLITE_CHECKPOINT_DB_PATH", value = "/data/checkpoints.db" },
    { name = "SQLITE_SIGNALS_DB_PATH", value = "/data/signals.db" },

    # Auth: local provider with a mounted PEM from Secrets Manager. Swap to oidc for real IdP.
    { name = "AUTH_PROVIDER", value = "local" },

    # Telemetry
    { name = "OTEL_ENABLED", value = tostring(local.otel_enabled) },
    { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = local.otel_enabled ? "http://localhost:4317" : "" },
    { name = "OTEL_SERVICE_NAME", value = "${var.name_prefix}-api" },
    { name = "OTEL_CAPTURE_PROMPT_CONTENT", value = "false" },
  ]

  # Secrets injected by the execution role at task start (design §18, tasks 20.6).
  api_secrets = [
    { name = "JWT_PRIVATE_KEY", valueFrom = aws_secretsmanager_secret.jwt_private_key.arn },
    { name = "TELEMETRY_CUSTOMER_HASH_SALT", valueFrom = aws_secretsmanager_secret.telemetry_hash_salt.arn },
  ]

  api_container = {
    name         = "api"
    image        = local.api_image
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = local.api_environment
    secrets      = local.api_secrets
    mountPoints  = [{ sourceVolume = "data", containerPath = "/data", readOnly = false }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  }

  # ADOT collector sidecar: receives OTLP on 4317 from the app and exports to X-Ray + CloudWatch.
  adot_container = {
    name      = "adot-collector"
    image     = local.adot_image
    essential = false
    command   = ["--config=/etc/ecs/ecs-default-config.yaml"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "adot"
      }
    }
  }
}

# ================================================================ api task + service
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode(
    concat([local.api_container], local.otel_enabled ? [local.adot_container] : [])
  )

  volume {
    name = "data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }
}

resource "aws_ecs_service" "api" {
  name            = "${var.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  # SQLite audit DB is single-writer (path A): never run two writers at once during a deploy.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http, aws_efs_mount_target.data]
}

# ================================================================ web task + service
resource "aws_ecs_task_definition" "web" {
  family                   = "${var.name_prefix}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web_cpu
  memory                   = var.web_memory
  execution_role_arn       = aws_iam_role.execution.arn

  container_definitions = jsonencode([
    {
      name         = "web"
      image        = local.web_image
      essential    = true
      portMappings = [{ containerPort = 8080, protocol = "tcp" }]
      environment = [
        { name = "API_UPSTREAM_HOST", value = aws_lb.main.dns_name },
        { name = "API_UPSTREAM_PORT", value = local.https_enabled ? "443" : "80" },
        { name = "NGINX_PORT", value = "8080" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.web.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "web"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "web" {
  name            = "${var.name_prefix}-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = var.web_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.web.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.http]
}

# ---------------------------------------------------------------- autoscaling (read/web path only)
# Path A caps the api WRITE path at one task (audit DB single-writer). Web scales freely.
resource "aws_appautoscaling_target" "web" {
  max_capacity       = 6
  min_capacity       = var.web_desired_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.web.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "web_cpu" {
  name               = "${var.name_prefix}-web-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.web.resource_id
  scalable_dimension = aws_appautoscaling_target.web.scalable_dimension
  service_namespace  = aws_appautoscaling_target.web.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 60
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}
