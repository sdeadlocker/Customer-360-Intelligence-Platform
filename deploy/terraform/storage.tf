# ---------------------------------------------------------------- EFS: writable data plane (path A)
# Path A bakes the read-only customer/knowledge DBs into the api image. The writable audit and
# checkpoint DBs live here so they survive a task replace. SQLite is single-writer, so api_desired_count
# stays at 1 for the write path (see variables.tf).
resource "aws_efs_file_system" "data" {
  creation_token   = "${var.name_prefix}-data"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = { Name = "${var.name_prefix}-data" }
}

resource "aws_efs_mount_target" "data" {
  count           = var.az_count
  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# Access point pins the POSIX identity to the non-root c360 uid/gid the api image runs as, and roots
# the export at /data so the container sees exactly its writable mount.
resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/data"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = { Name = "${var.name_prefix}-data-ap" }
}

# ---------------------------------------------------------------- S3: reports and generated assets
resource "aws_s3_bucket" "reports" {
  bucket = "${var.name_prefix}-reports-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ---------------------------------------------------------------- Secrets Manager
# Values are placeholders created by Terraform; set the real values out of band (console/CLI) so no
# secret ever lands in state from a var. The task role reads these at runtime (design §18, tasks 20.6).
resource "aws_secretsmanager_secret" "jwt_private_key" {
  name        = "c360/jwt-private-key"
  description = "RSA PEM used to sign local-provider JWTs. Replace the placeholder with a real key, or switch AUTH_PROVIDER=oidc."
}

resource "aws_secretsmanager_secret" "telemetry_hash_salt" {
  name        = "c360/telemetry-hash-salt"
  description = "Salt for hashing customer IDs before they reach telemetry (required in staging/prod)."
}
