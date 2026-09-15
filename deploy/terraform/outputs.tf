output "alb_dns_name" {
  description = "Public DNS of the load balancer. Point your domain's CNAME/ALIAS here."
  value       = aws_lb.main.dns_name
}

output "app_url" {
  description = "The application URL once DNS/TLS are in place."
  value       = var.domain_name != "" ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}"
}

output "ecr_api_repository_url" {
  description = "Push the api image here."
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_web_repository_url" {
  description = "Push the web image here."
  value       = aws_ecr_repository.web.repository_url
}

output "reports_bucket" {
  value = aws_s3_bucket.reports.bucket
}

output "efs_file_system_id" {
  value = aws_efs_file_system.data.id
}

output "task_role_arn" {
  description = "The least-privilege runtime role the api assumes (Bedrock + secrets + S3 + EFS + telemetry)."
  value       = aws_iam_role.task.arn
}

output "jwt_secret_arn" {
  value = aws_secretsmanager_secret.jwt_private_key.arn
}

output "telemetry_salt_secret_arn" {
  value = aws_secretsmanager_secret.telemetry_hash_salt.arn
}

output "acm_certificate_validation_records" {
  description = "DNS records to create for ACM validation when a certificate was provisioned by Terraform."
  value = var.domain_name != "" && var.acm_certificate_arn == "" ? {
    for dvo in aws_acm_certificate.main[0].domain_validation_options :
    dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  } : {}
}
