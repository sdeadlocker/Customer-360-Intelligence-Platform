#!/usr/bin/env bash
# Populate the runtime secrets that Terraform created as empty placeholders.
#
# - c360/jwt-private-key    : an RSA PEM used by the local auth provider to sign JWTs.
# - c360/telemetry-hash-salt: a random salt for hashing customer IDs before telemetry.
#
# Run once after `terraform apply`. Values go straight into Secrets Manager — never into git or state.
#
# Usage: REGION=us-east-1 ./deploy/scripts/set-secrets.sh
set -euo pipefail

REGION="${REGION:-us-east-1}"

echo "==> Generating an RSA private key for JWT signing"
JWT_PEM="$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null)"

echo "==> Storing c360/jwt-private-key"
aws secretsmanager put-secret-value \
  --region "$REGION" \
  --secret-id "c360/jwt-private-key" \
  --secret-string "$JWT_PEM" >/dev/null

echo "==> Generating and storing c360/telemetry-hash-salt"
SALT="$(openssl rand -hex 32)"
aws secretsmanager put-secret-value \
  --region "$REGION" \
  --secret-id "c360/telemetry-hash-salt" \
  --secret-string "$SALT" >/dev/null

echo "Done. Force a new deployment to pick up the values:"
echo "  aws ecs update-service --region $REGION --cluster c360-cluster --service c360-api --force-new-deployment"
