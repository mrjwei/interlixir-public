#!/usr/bin/env bash
# Build a service's image for linux/arm64 and push it to its ECR repo, tagged by git SHA (never `latest`).
# Usage: scripts/push_image.sh <service>   (e.g. api) -> <account>.dkr.ecr.ap-southeast-2.amazonaws.com/interlixir-<service>:<sha>
# The repo must already exist (created in the console). Point a new task definition revision at the printed URI.
# With uncommitted changes the tag gets a -dirty suffix, so a pushed tag always names what it was built from.
# services/shared is passed as the named build context "shared" (`COPY --from=shared ...`); images that
# don't use it are unaffected.
set -euo pipefail

service="${1:?usage: $0 <service>}"
root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/services/$service"
[[ -f "$src/Dockerfile" ]] || { echo "no Dockerfile in $src" >&2; exit 1; }

region="ap-southeast-2"
account="$(aws sts get-caller-identity --query Account --output text)"
registry="$account.dkr.ecr.$region.amazonaws.com"
repo="interlixir-$service"

tag="$(git -C "$root" rev-parse --short=12 HEAD)"
[[ -z "$(git -C "$root" status --porcelain -- "services/$service" services/shared)" ]] || tag="$tag-dirty"
uri="$registry/$repo:$tag"

aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
# --provenance=false keeps the push a single image manifest instead of an index plus an attestation.
docker buildx build --platform linux/arm64 --provenance=false --build-context shared="$root/services/shared" \
  -t "$uri" --push "$src"
echo "$uri"
