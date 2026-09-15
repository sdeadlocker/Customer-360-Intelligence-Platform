terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Configure a remote backend before team use. Left local by default so a first `terraform init`
  # works with no prior setup. Example S3 backend:
  #
  # backend "s3" {
  #   bucket         = "c360-tfstate-<account_id>"
  #   key            = "customer-360/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "c360-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "customer-360"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
