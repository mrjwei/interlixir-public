#!/usr/bin/env bash
# Build the SPA and publish it: sync web/dist to the web bucket, then invalidate index.html on CloudFront.
# Usage: CLOUDFRONT_DISTRIBUTION_ID=E... scripts/deploy_web.sh
# Hashed files under assets/ are immutable (cached a year, never deleted here, so a browser still holding an
# older index.html can load its assets). Everything else, i.e. index.html and favicon.svg, is no-cache and
# replaced, and only /index.html and / need an invalidation.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
bucket="${WEB_BUCKET:-interlixir-web}"
distribution="${CLOUDFRONT_DISTRIBUTION_ID:?set CLOUDFRONT_DISTRIBUTION_ID}"

cd "$root/web"
npm ci
npm run build

aws s3 sync dist/assets "s3://$bucket/assets" --cache-control "public, max-age=31536000, immutable"
aws s3 sync dist "s3://$bucket" --exclude "assets/*" --delete --cache-control "no-cache"
aws cloudfront create-invalidation --distribution-id "$distribution" --paths "/index.html" "/" \
  --query "Invalidation.{id:Id,status:Status}" --output text
