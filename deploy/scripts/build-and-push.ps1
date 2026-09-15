<#
.SYNOPSIS
  Build the Customer 360 api and web images and push them to ECR, tagged by git revision.

.DESCRIPTION
  Path A build: the api image bakes the seeded read-only customer + knowledge databases. By default
  the knowledge index uses mock embeddings (hermetic, offline). Pass -EmbedProvider bedrock to bake
  real Titan embeddings (needs AWS credentials as a BuildKit secret; RAG retrieval quality depends on
  this). Generation (agents) is always real Bedrock at runtime regardless of this build choice.

.PARAMETER Region
  AWS region hosting the ECR repositories (must match the Terraform `region`).

.PARAMETER AccountId
  Target AWS account ID. Defaults to the caller identity.

.PARAMETER NamePrefix
  Resource name prefix. Must match Terraform `name_prefix` (default c360).

.PARAMETER Tag
  Image tag. Defaults to the git short SHA.

.PARAMETER EmbedProvider
  mock (default) or bedrock — controls how the knowledge index is baked into the api image.

.EXAMPLE
  ./deploy/scripts/build-and-push.ps1 -Region us-east-1
#>
[CmdletBinding()]
param(
  [string]$Region = "us-east-1",
  [string]$AccountId = "",
  [string]$NamePrefix = "c360",
  [string]$Tag = "",
  [ValidateSet("mock", "bedrock")]
  [string]$EmbedProvider = "mock"
)

$ErrorActionPreference = "Stop"

# Repo root is two levels up from this script (deploy/scripts).
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not $AccountId) {
  $AccountId = (aws sts get-caller-identity --query Account --output text).Trim()
}
if (-not $Tag) {
  $Tag = (git -C $RepoRoot rev-parse --short HEAD).Trim()
}

$Registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$ApiRepo = "$Registry/$NamePrefix-api"
$WebRepo = "$Registry/$NamePrefix-web"

Write-Host "Registry : $Registry"
Write-Host "Tag      : $Tag"
Write-Host "Embed    : $EmbedProvider"
Write-Host ""

# ECR login.
Write-Host "==> ECR login"
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Registry

$env:DOCKER_BUILDKIT = "1"

# ---- api image ----
Write-Host "==> Build api ($EmbedProvider embeddings)"
$apiBuildArgs = @(
  "build",
  "-f", "docker/Dockerfile.api",
  "--build-arg", "EMBED_PROVIDER=$EmbedProvider",
  "-t", "${ApiRepo}:$Tag",
  "-t", "${ApiRepo}:latest"
)
if ($EmbedProvider -eq "bedrock") {
  # Bake real Titan embeddings: the ingest RUN reads AWS creds from this BuildKit secret.
  $credPath = Join-Path $env:USERPROFILE ".aws\credentials"
  $apiBuildArgs += @("--secret", "id=aws,src=$credPath")
}
$apiBuildArgs += "."
docker @apiBuildArgs

# ---- web image ----
Write-Host "==> Build web"
docker build -f docker/Dockerfile.web -t "${WebRepo}:$Tag" -t "${WebRepo}:latest" .

# ---- push ----
Write-Host "==> Push api"
docker push "${ApiRepo}:$Tag"
docker push "${ApiRepo}:latest"

Write-Host "==> Push web"
docker push "${WebRepo}:$Tag"
docker push "${WebRepo}:latest"

Write-Host ""
Write-Host "Done. Deploy this build with:"
Write-Host "  terraform -chdir=deploy/terraform apply -var=api_image_tag=$Tag -var=web_image_tag=$Tag"
