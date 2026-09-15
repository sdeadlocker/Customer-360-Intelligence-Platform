variable "region" {
  description = "AWS region to deploy into. Must have Bedrock model access enabled for the chosen models."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment label (dev | staging | prod)."
  type        = string
  default     = "prod"
}

variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
  default     = "c360"
}

# ---------------------------------------------------------------- networking
variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "Number of Availability Zones to spread public/private subnets across."
  type        = number
  default     = 2
}

variable "domain_name" {
  description = "Fully qualified domain name for the public HTTPS endpoint (e.g. c360.example.com). Leave empty to skip ACM/HTTPS and expose HTTP on the ALB (dev only)."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ARN of an existing ACM certificate for domain_name. If empty and domain_name is set, a certificate is created (DNS validation must be completed out of band)."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------- images
variable "api_image_tag" {
  description = "Tag of the c360-api image to deploy (typically the git short SHA)."
  type        = string
  default     = "latest"
}

variable "web_image_tag" {
  description = "Tag of the c360-web image to deploy (typically the git short SHA)."
  type        = string
  default     = "latest"
}

# ---------------------------------------------------------------- compute sizing
variable "api_cpu" {
  description = "Fargate CPU units for the api task (256 = 0.25 vCPU)."
  type        = number
  default     = 1024
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the api task."
  type        = number
  default     = 2048
}

variable "web_cpu" {
  type    = number
  default = 256
}

variable "web_memory" {
  type    = number
  default = 512
}

variable "api_desired_count" {
  description = "Number of api tasks. Path A caps the WRITE path at one writer (audit DB is single-writer); scale the read path with care or move to path B (RDS)."
  type        = number
  default     = 1
}

variable "web_desired_count" {
  type    = number
  default = 2
}

# ---------------------------------------------------------------- Bedrock / LLM
variable "llm_provider" {
  description = "mock (no AWS calls) or bedrock (live LLM for agents + RAG)."
  type        = string
  default     = "bedrock"

  validation {
    condition     = contains(["mock", "bedrock"], var.llm_provider)
    error_message = "llm_provider must be 'mock' or 'bedrock'."
  }
}

variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile ID for generation (agents / Q&A). Claude 4.5 is reached via a us. cross-region inference profile."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_embed_model_id" {
  description = "Bedrock embedding model ID for RAG (retrieval + ingest)."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "bedrock_embed_dimensions" {
  description = "Titan embedding dimensionality (256 | 512 | 1024). Must match what the knowledge index was baked with."
  type        = number
  default     = 1024
}

variable "bedrock_guardrail_id" {
  description = "Optional Bedrock Guardrail ID applied to generation. Empty disables guardrails."
  type        = string
  default     = ""
}

variable "bedrock_model_arns" {
  description = "Explicit foundation-model + inference-profile ARNs the task role may invoke. Rendered into the least-privilege task policy. {region}/{account_id} are interpolated by the module."
  type        = list(string)
  default = [
    "arn:aws:bedrock:{region}::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    "arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
    "arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0",
    "arn:aws:bedrock:{region}:{account_id}:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "arn:aws:bedrock:{region}:{account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
    "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
    "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"
  ]
}

# ---------------------------------------------------------------- observability / app
variable "otel_enabled" {
  description = "Run the ADOT collector sidecar and export traces/metrics to X-Ray/CloudWatch."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch log group retention."
  type        = number
  default     = 30
}
