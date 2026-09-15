#!/usr/bin/env bash
# Build the Customer 360 api and web images and push them to ECR, tagged by git revision.
#
# Path A build: the api image bakes the seeded read-only customer + knowledge databases. By default
# the knowledge index uses mock embeddings (hermetic, offline). Set EMBED_PROVIDER=bedrock to bake
# real Titan embeddings (needs AWS credentials as a BuildKit secret; RAG retrieval quality depends on
# it). Generation (agents) is always real Bedrock at runtime regardless of this build choice.
#
# Usage:
#   REGION=us-east-1 ./deploy/scripts/build-and-push.sh
#   EMBED_PROVIDER=bedrock REGION=us-east-1 ./deploy/scripts/build-and-push.sh
set -euo pipefail

REGION="${REGION:-us-east-1}"
NAME_PREFIX="${NAME_PREFIX:-c360}"
EMBED_PROVIDER="${EMBED_PROVIDER:-mock}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
API_REPO="${REGISTRY}/${NAME_PREFIX}-api"
WEB_REPO="${REGISTRY}/${NAME_PREFIX}-web"

echo "Registry : ${REGISTRY}"
echo "Tag      : ${TAG}"
echo "Embed    : ${EMBED_PROVIDER}"
echo

echo "==> ECR login"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

export DOCKER_BUILDKIT=1

echo "==> Build api (${EMBED_PROVIDER} embeddings)"
API_BUILD_ARGS=(
  build
  -f docker/Dockerfile.api
  --build-arg "EMBED_PROVIDER=${EMBED_PROVIDER}"
  -t "${API_REPO}:${TAG}"
  -t "${API_REPO}:latest"
)
if [[ "$EMBED_PROVIDER" == "bedrock" ]]; then
  API_BUILD_ARGS+=(--secret "id=aws,src=${HOME}/.aws/credentials")
fi
API_BUILD_ARGS+=(.)
docker "${API_BUILD_ARGS[@]}"

echo "==> Build web"
docker build -f docker/Dockerfile.web -t "${WEB_REPO}:${TAG}" -t "${WEB_REPO}:latest" .

echo "==> Push api"
docker push "${API_REPO}:${TAG}"
docker push "${API_REPO}:latest"

echo "==> Push web"
docker push "${WEB_REPO}:${TAG}"
docker push "${WEB_REPO}:latest"

echo
echo "Done. Deploy this build with:"
echo "  terraform -chdir=deploy/terraform apply -var=api_image_tag=${TAG} -var=web_image_tag=${TAG}"
