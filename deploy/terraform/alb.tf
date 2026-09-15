resource "aws_lb" "main" {
  name               = "${var.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "${var.name_prefix}-alb" }
}

# Web target group: nginx serves the SPA on 8080.
resource "aws_lb_target_group" "web" {
  name        = "${var.name_prefix}-web-tg"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/"
    matcher             = "200-399"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# API target group: FastAPI on 8000. Readiness is /ready; a task failing it is not routed to.
resource "aws_lb_target_group" "api" {
  name        = "${var.name_prefix}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/ready"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # SSE endpoints (/insights, /recommendations, /ask) stream; keep the connection alive.
  stickiness {
    type            = "lb_cookie"
    enabled         = false
    cookie_duration = 86400
  }
}

# ---------------------------------------------------------------- ACM (optional)
resource "aws_acm_certificate" "main" {
  count             = var.domain_name != "" && var.acm_certificate_arn == "" ? 1 : 0
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  https_enabled   = var.domain_name != ""
  certificate_arn = var.acm_certificate_arn != "" ? var.acm_certificate_arn : (local.https_enabled ? aws_acm_certificate.main[0].arn : "")
}

# ---------------------------------------------------------------- listeners
# HTTPS listener when a domain/cert is configured; default action serves the web SPA. The API is
# reached same-origin under /api/* etc., which nginx proxies to the api service — but we also route
# the API paths at the ALB so the api target group receives them directly.
resource "aws_lb_listener" "https" {
  count             = local.https_enabled ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# HTTP: redirect to HTTPS when TLS is on; otherwise serve the web SPA directly (dev only).
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = local.https_enabled ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = local.https_enabled ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.web.arn
    }
  }
}

# Route the API surface to the api target group. These are the FastAPI path prefixes.
locals {
  api_path_patterns = ["/api/*", "/customers/*", "/ask", "/search", "/insights", "/recommendations", "/health", "/ready", "/auth/*", "/openapi.json", "/docs"]
  active_listener   = local.https_enabled ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = local.active_listener
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    path_pattern {
      values = local.api_path_patterns
    }
  }
}
