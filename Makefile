# Entry points for the steps that repeat. Resources themselves are created in the AWS console;
# see each service's README.

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: check-schemas
check-schemas: ## Meta-validate schemas/builtin/*.json before seeding
	python3 scripts/check_schemas.py schemas/builtin

.PHONY: test-contracts
test-contracts: ## Contract tests for the event, message and log contracts in services/shared/contracts
	python3 -m unittest discover -s services/shared/tests -t services/shared -v

.PHONY: test-stream-router
test-stream-router: ## Unit tests for the stream router, incl. its filter criteria
	PYTHONPATH=services/shared:services/stream-router python3 -m unittest discover -s services/stream-router/tests -v -b

.PHONY: zip-stream-router
zip-stream-router: ## Build dist/stream-router.zip (handler + shared contract) for the Lambda
	scripts/build_lambda_zip.sh stream-router

API_VENV := services/api/.venv

.PHONY: venv-api
venv-api: ## Create services/api/.venv (Python 3.14 via uv) with the API's test dependencies
	uv venv --python 3.14 $(API_VENV)
	VIRTUAL_ENV=$(API_VENV) uv pip install -r services/api/requirements-dev.txt

.PHONY: test-api
test-api: ## API service tests against moto: routes, user scoping, retry, cursors, upload policies, cache (MEMCACHED_TEST_ENDPOINT=host:port adds a real server)
	PYTHONPATH=services/shared $(API_VENV)/bin/python -m unittest discover -s services/api/tests -t services/api -v -b

.PHONY: push-api
push-api: ## Build the API image (linux/arm64) and push it to ECR, tagged by git SHA
	scripts/push_image.sh api

JOB_CREATOR_VENV := services/job-creator/.venv

.PHONY: venv-job-creator
venv-job-creator: ## Create services/job-creator/.venv (Python 3.14 via uv) with its test dependencies
	uv venv --python 3.14 $(JOB_CREATOR_VENV)
	VIRTUAL_ENV=$(JOB_CREATOR_VENV) uv pip install -r services/job-creator/requirements-dev.txt

.PHONY: test-job-creator
test-job-creator: ## Job creator tests against moto, plus ffmpeg chunking tests (need ffmpeg on PATH)
	PYTHONPATH=services/shared:services/job-creator $(JOB_CREATOR_VENV)/bin/python -m unittest discover -s services/job-creator/tests -v -b

.PHONY: push-job-creator
push-job-creator: ## Build the job creator Lambda image (linux/arm64, with ffmpeg) and push it to ECR, tagged by git SHA
	scripts/push_image.sh job-creator

WORKER_VENV := services/whisper-worker/.venv

.PHONY: venv-whisper-worker
venv-whisper-worker: ## Create services/whisper-worker/.venv (Python 3.14 via uv) with faster-whisper and the test dependencies
	uv venv --python 3.14 $(WORKER_VENV)
	VIRTUAL_ENV=$(WORKER_VENV) uv pip install -r services/whisper-worker/requirements-dev.txt

.PHONY: test-whisper-worker
test-whisper-worker: ## Whisper worker tests against moto; the real-model tests run only if the model is cached
	PYTHONPATH=services/shared:services/whisper-worker $(WORKER_VENV)/bin/python -m unittest discover -s services/whisper-worker/tests -v -b

.PHONY: push-whisper-worker
push-whisper-worker: ## Build the Whisper worker image (linux/arm64, model baked in) and push it to ECR, tagged by git SHA
	scripts/push_image.sh whisper-worker

SYNTHESIS_VENV := services/synthesis/.venv

.PHONY: venv-synthesis
venv-synthesis: ## Create services/synthesis/.venv (Python 3.14 via uv) with jsonschema, pypdf and the test dependencies
	uv venv --python 3.14 $(SYNTHESIS_VENV)
	VIRTUAL_ENV=$(SYNTHESIS_VENV) uv pip install -r services/synthesis/requirements-dev.txt

.PHONY: test-synthesis
test-synthesis: ## Synthesis tests against moto with a fake Bedrock: steps, resume, idempotency, failures, redaction, rendering
	PYTHONPATH=services/shared:services/synthesis $(SYNTHESIS_VENV)/bin/python -m unittest discover -s services/synthesis/tests -v -b

.PHONY: zip-synthesis
zip-synthesis: ## Build dist/synthesis.zip (handler modules + shared contract + arm64 jsonschema/pypdf wheels) for the Lambda
	scripts/build_lambda_zip.sh synthesis


.PHONY: seed-schemas
seed-schemas: check-schemas ## Re-seed the 6 SCHEMA#BUILTIN items from schemas/builtin/: dry run, ARGS=--apply writes
	$(SYNTHESIS_VENV)/bin/python scripts/seed_schemas.py $(ARGS)

DLQ_VENV := services/dlq-handler/.venv

.PHONY: venv-dlq-handler
venv-dlq-handler: ## Create services/dlq-handler/.venv (Python 3.14 via uv) with its test dependencies
	uv venv --python 3.14 $(DLQ_VENV)
	VIRTUAL_ENV=$(DLQ_VENV) uv pip install -r services/dlq-handler/requirements-dev.txt

.PHONY: test-dlq-handler
test-dlq-handler: ## Dead-letter handler tests against moto: each DLQ's message -> Failed + JobFailed, stale ones ignored
	PYTHONPATH=services/shared:services/dlq-handler $(DLQ_VENV)/bin/python -m unittest discover -s services/dlq-handler/tests -v -b

.PHONY: zip-dlq-handler
zip-dlq-handler: ## Build dist/dlq-handler.zip (handler + shared contract) for the Lambda
	scripts/build_lambda_zip.sh dlq-handler

HISTORY_VENV := services/history-writer/.venv

.PHONY: venv-history-writer
venv-history-writer: ## Create services/history-writer/.venv (Python 3.14 via uv) with its test dependencies
	uv venv --python 3.14 $(HISTORY_VENV)
	VIRTUAL_ENV=$(HISTORY_VENV) uv pip install -r services/history-writer/requirements-dev.txt

.PHONY: test-history-writer
test-history-writer: ## History writer tests against moto: one Event item per history event, deduped on eventId
	PYTHONPATH=services/shared:services/history-writer $(HISTORY_VENV)/bin/python -m unittest discover -s services/history-writer/tests -v -b

.PHONY: zip-history-writer
zip-history-writer: ## Build dist/history-writer.zip (handler + shared contract) for the Lambda
	scripts/build_lambda_zip.sh history-writer

GC_VENV := services/gc/.venv

.PHONY: venv-gc
venv-gc: ## Create services/gc/.venv (Python 3.14 via uv) with its test dependencies
	uv venv --python 3.14 $(GC_VENV)
	VIRTUAL_ENV=$(GC_VENV) uv pip install -r services/gc/requirements-dev.txt

.PHONY: test-gc
test-gc: ## GC tests against moto: a deleted project or document leaves no objects or items, re-runs are no-ops, other users untouched
	PYTHONPATH=services/shared:services/gc $(GC_VENV)/bin/python -m unittest discover -s services/gc/tests -v -b

.PHONY: zip-gc
zip-gc: ## Build dist/gc.zip (handler + shared contract) for the Lambda
	scripts/build_lambda_zip.sh gc

CHAT_VENV := services/chat-agent/.venv

.PHONY: venv-chat
venv-chat: ## Create services/chat-agent/.venv (Python 3.14 via uv) for both the chat agent and the MCP server, with test dependencies
	uv venv --python 3.14 $(CHAT_VENV)
	VIRTUAL_ENV=$(CHAT_VENV) uv pip install -r services/chat-agent/requirements-dev.txt

.PHONY: test-mcp-server
test-mcp-server: ## MCP server tests against moto over real Streamable HTTP: every tool, user scoping, token checks
	PYTHONPATH=services/shared $(CHAT_VENV)/bin/python -m unittest discover -s services/mcp-server/tests -t services/mcp-server -v -b

.PHONY: test-chat-agent
test-chat-agent: ## Chat agent tests against moto with the real MCP server and a scripted Bedrock: tool loop, isolation, sessions, quota, SSE
	PYTHONPATH=services/shared:services/mcp-server $(CHAT_VENV)/bin/python -m unittest discover -s services/chat-agent/tests -t services/chat-agent -v -b

.PHONY: push-chat-agent
push-chat-agent: ## Build the chat agent image (linux/arm64) and push it to ECR, tagged by git SHA
	scripts/push_image.sh chat-agent

.PHONY: push-mcp-server
push-mcp-server: ## Build the MCP server sidecar image (linux/arm64) and push it to ECR, tagged by git SHA
	scripts/push_image.sh mcp-server


.PHONY: dev-web
dev-web: ## Run the SPA on http://localhost:5173, proxying /a3 and /v1/auth to the real API
	cd web && npm install && npm run dev

.PHONY: build-web
build-web: ## Type-check, lint and build the SPA into web/dist
	cd web && npm ci && npm run lint && npm run build

.PHONY: deploy-web
deploy-web: ## Build the SPA, sync to the web bucket, invalidate CloudFront (needs CLOUDFRONT_DISTRIBUTION_ID)
	scripts/deploy_web.sh
