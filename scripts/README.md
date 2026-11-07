# scripts/

Steps that repeat, exposed through the root `Makefile`.

| Script | Make target | What it does |
| --- | --- | --- |
| `check_schemas.py` | `make check-schemas` | Meta-validates `schemas/builtin/*.json`; also run in CI |
| `seed_schemas.py` | `make seed-schemas` | Re-seeds the six built-in schema items into DynamoDB, byte for byte. Dry run by default; `ARGS=--apply` writes |
| `build_lambda_zip.sh` | `make zip-*` | Builds the Lambda zips into `dist/` (handler, shared contracts and arm64 wheels) |
| `push_image.sh <service>` | `make push-*` | Builds a `linux/arm64` image and pushes it to ECR, tagged by git SHA |
| `deploy_web.sh` | `make deploy-web` | Builds `web/`, syncs it to the web bucket and invalidates CloudFront (needs `CLOUDFRONT_DISTRIBUTION_ID`) |
