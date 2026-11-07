# services/chat-agent

The Ask AI agent (`interlixir-chat-agent`, FastAPI on port 8080): one ECS Fargate task in the private subnets with **two containers**, this agent and the MCP server sidecar (`services/mcp-server/`). Path: SPA → CloudFront `/acp/*` (adds the origin secret header) → the shared ALB, app host, rule `/acp/*` + secret → target group → this container. Milestone **M18**.

```
browser ──/acp/messages──▶ CloudFront ──X-A3-Origin-Secret──▶ ALB rule 4 ──▶ agent :8080 ──Converse──▶ Bedrock (Nemotron)
                                                                               │  ▲
                                                           Bearer <ID token>   ▼  │ tool results
                                                                         mcp-server 127.0.0.1:8081 ──▶ DynamoDB, S3 docs
                                                                               (same task, loopback only)
agent ──quota incr──▶ Memcached (M12 node)        agent ──ChatSession──▶ EventBridge bus ──▶ history writer
```

**Rules (review checklist)**
- **The token is checked on every request, here and again in MCP.** On `/acp` nothing upstream checks the user (CloudFront and the ALB only check the request came through CloudFront), so `auth.py` is the only authentication: Cognito JWKS, RS256, issuer, `aud` = the app client, `token_use = id`, `exp`, a key-safe `sub`. The raw token goes to the MCP server as the bearer, which verifies it again and takes the userId from it.
- **The model never sees or chooses a userId.** No tool takes one; tool arguments are only project and document IDs, which the MCP server checks belong to the caller (404-style "No such …" otherwise).
- **Sessions are owned by their key** (`USER#<userId>` / `CHAT#<id>`); messages are read only after the session lookup. Someone else's session is 404, never 403.
- **Bounded per message:** at most 6 tool rounds, 2000 output tokens per model call, tool results cut at 40,000 characters, the last 12 messages as history. With 30 messages per hour, that caps what one user can spend.
- Document text reaches the model as tool results, and the system prompt says it's data, never instructions. The MCP server has no write path, so an injected instruction can only make the agent read more of the caller's own data.

## Layout

| File | What |
| --- | --- |
| `agent/main.py` | The routes, the SSE stream with heartbeats, the request log line |
| `agent/auth.py` | `CallerDep`: the verified `userId` plus the raw token (forwarded to MCP, never logged) |
| `agent/chat.py` | One turn: MCP tools listed, the Converse tool loop, limits, the metered line per Bedrock call (`LlmTokens`/`LlmCost` by `Model`, like synthesis) |
| `agent/store.py` | ChatSession / ChatMessage items, delete, and the `ChatSession` event |
| `agent/quota.py` | The hourly counter in Memcached (M12's `count_in_window`, same key scheme), warning and block flags |
| `tests/test_agent.py` | The routes end to end against moto, the **real MCP server** on a local port, real token checks, a scripted Bedrock and an in-memory Memcached |
| `Dockerfile` | `python:3.14-slim`, `nobody`, `uvicorn agent.main:app` on 8080. Build for `linux/arm64` (`make push-chat-agent`) |

## Routes

All need `Authorization: Bearer <Cognito ID token>` (401 otherwise). IDs match `^[A-Za-z0-9_-]{1,128}$` (422 otherwise).

| Method and path | Does |
| --- | --- |
| `GET /healthz` | `{"status":"ok"}` for the target group. No dependency calls |
| `GET /acp/quota` | This hour's count without adding to it: `{used, limit, resetAt, warning, blocked}`. `used` is `null` when the cache can't be read |
| `GET /acp/sessions` | The caller's sessions, most recently used first (≤ 50): `sessionId`, `title`, `createdAt`, `updatedAt`, `messageCount`, `tokenCount`, `cost` |
| `GET /acp/sessions/{sessionId}` | The session plus `messages` (`messageId`, `role`, `text`, `sentAt`; assistant: `tools`, `tokenCount`, `cost`) |
| `DELETE /acp/sessions/{sessionId}` | 204. The session item first (it leaves the list at once), then its messages |
| `POST /acp/messages` | Body `{text (1–2000 chars), sessionId?}`. No `sessionId` starts a session titled from the text. **429** `{detail, quota}` with `Retry-After` over the quota (the model isn't called). Otherwise a `text/event-stream` |

**The stream.** Every event's `data:` is one JSON object, in this order: `session` `{sessionId, title, created}` → `quota` → `tool` `{id, name, status: running|done|error, label?}` per call (label: the document title or project name the result named, for the UI chip) → `message` (the saved assistant message) or `error` `{detail}` → `done`. A `: ping` comment goes out every 15 s while the model or a tool works, so CloudFront's 30 s origin read timeout never sees an idle connection. The turn runs in its own task: a browser that leaves mid-answer doesn't cancel it, and the answer is saved for when the session is reopened.

**Writes per message.** The user message is written (with `messageCount + 1` on its session) before the model is called, so a failed turn still shows the question. The answer is written with its tokens and cost added to the session. Each write is one transaction of a Put and a conditional Update on the session (no `ConditionCheckItem`), so a session deleted mid-turn drops the answer instead of leaving orphan messages. Then one **`ChatSession` event per answered message**: `eventId` = the assistant message's ID, `chatSessionId`, the turn's `tokenCount` and `cost`. The history writer makes it a "Chat message" row, which reopens the chat in the panel.

**Quota.** `quota:<userId>:<yyyymmddhh>` in the M12 Memcached node, created by `add` with a TTL just past the hour, then `incr` (atomic across tasks). Counted when a message is accepted (after the session check, so a wrong ID costs nothing). `warning` from 24 of 30 (80%, rounded up), `blocked` once 30 are used, and the 31st is 429 with `resetAt` = the next hour boundary. **Fails open**: without a usable cache the message is allowed, `used` is `null`, and a `quota unavailable, message not metered` warning is logged. That keeps chat up through a cache outage; the per-message limits above still bound the cost.

## Config

Environment variables with the real names as defaults: `TABLE_NAME`, `EVENT_BUS_NAME` (`interlixir-bus`), `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `CHAT_MODEL_ID` (`nvidia.nemotron-super-3-120b`), `MCP_URL` (`http://127.0.0.1:8081/mcp`), `CHAT_MESSAGES_PER_HOUR` (30), `AWS_REGION`. Set in the task definition: **`CACHE_ENDPOINT`** and **`CACHE_TLS=true`** (the same values as the API task), and, since M21, nothing for prices: the table is read from Parameter Store `/interlixir/model-prices` (`services/synthesis/README.md`, Price table). `MODEL_PRICES` is only a fallback if that read has never worked. **The task role needs `ssm:GetParameter` on the parameter.**

## Run and test

```bash
make venv-chat && make test-mcp-server && make test-chat-agent
```

## Console setup

Everything is tagged `project = interlixir`, and recorded in the deployment notes. Minimum settings only: leave every optional setting at its default.

1. **ECR → Create repository** twice: `interlixir-chat-agent` and `interlixir-mcp-server` (private, defaults, both tags). Then `make push-chat-agent` and `make push-mcp-server`; note both printed URIs.
2. **Systems Manager → Parameter Store → Create parameter** `/interlixir/cloudfront-secret`: SecureString, a new random value of 40+ characters (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), both tags. It goes into the ALB rule (step 5) and the CloudFront origin (step 7); change all three together.
3. **ECS → Task definitions → Create new task definition** `interlixir-chat-agent`: Fargate, **Linux/ARM64**, **0.25 vCPU / 0.5 GB**, task role `ecs-task-role`, task execution role `ecs-execution-role`, both tags.
   - Container 1 **`agent`**: the chat-agent image URI, **essential**, port mapping **8080/TCP**. Environment: `CACHE_ENDPOINT` = the API task's value, `CACHE_TLS` = `true`, `MODEL_PRICES` = the synthesis function's value. Log collection on (awslogs), group `/ecs/interlixir-chat-agent`, stream prefix `agent`. Startup dependency: `mcp` **START**.
   - Container 2 **`mcp`**: the mcp-server image URI, **essential**, **no port mappings** (it listens on 127.0.0.1 only), same log group, stream prefix `mcp`. No environment (code defaults).
4. **EC2 → Target groups → Create**: `interlixir-chat-tg`, target type **IP addresses**, HTTP **8080**, the shared VPC, health check path **`/healthz`**. Register no targets (the service does). Both tags.
5. **EC2 → Load balancers → `interlixir-alb` → HTTPS:443 listener → Add rule**, name `interlixir-acp`, priority **4** (before the app rules 5 and 6; the shared platform's 10/20/30 are unaffected because every A3 rule also matches the app host). Conditions: host header `app.example.com` **and** path `/acp/*` **and** HTTP header `X-A3-Origin-Secret` = the parameter's value. Action: forward to `interlixir-chat-tg`. Both tags. Rule 6 (host only → 403) still catches an `/acp` request without the header.
6. **ECS → `interlixir-ecs-cluster` → Services → Create**: from the task definition above, capacity provider FARGATE, service `interlixir-chat-agent`, desired **1**, private subnets **1A/2A/3A**, public IP **off**, security group **`AppSG`** only (it reaches Memcached through `CacheSG`, and the ALB through the same group as the API task), load balancing: the existing ALB, container `agent:8080`, the existing target group `interlixir-chat-tg`. Deployment circuit breaker with rollback on, propagate tags from the task definition, both tags.
7. **CloudFront → `<distribution-id>` → Origins → Create origin**: domain **`app.example.com`**, protocol **HTTPS only**, minimum origin TLS 1.2, **Add custom header** `X-A3-Origin-Secret` = the parameter's value. Response timeout and keep-alive at their defaults (30 s and 5 s; the heartbeats keep the stream under both). Name `interlixir-acp`.
8. **Behaviours → Create behaviour**: path pattern **`/acp/*`** → that origin, viewer protocol redirect HTTP to HTTPS, allowed methods **GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE**, cache policy **`Managed-CachingDisabled`**, origin request policy **`Managed-AllViewerExceptHostHeader`**, no function associations. Check its precedence is above the default `*` behaviour. Wait for the deployment (a few minutes).
9. `CLOUDFRONT_DISTRIBUTION_ID=<distribution-id> make deploy-web` (the SPA with the Ask AI panel), then `make check-chat | tee the build logs`.

Read back (all read-only):

```bash
aws ecs describe-services --cluster interlixir-ecs-cluster --services interlixir-chat-agent \
  --query 'services[0].{status:status,running:runningCount,taskDef:taskDefinition,net:networkConfiguration}'
aws ecs describe-task-definition --task-definition interlixir-chat-agent \
  --query 'taskDefinition.{cpu:cpu,mem:memory,arch:runtimePlatform,containers:containerDefinitions[].{name:name,image:image,ports:portMappings,dependsOn:dependsOn}}'
aws elbv2 describe-target-health --target-group-arn <chat tg arn>
aws elbv2 describe-rules --listener-arn <HTTPS listener arn> --query 'Rules[?Priority==`4`]'
aws cloudfront get-distribution-config --id <distribution-id> \
  --query 'DistributionConfig.{origins:Origins.Items[].DomainName,behaviours:CacheBehaviors.Items[].{path:PathPattern,origin:TargetOriginId}}'
aws logs tail /ecs/interlixir-chat-agent --since 10m
```

## Pitfalls

- **Forward all headers except Host.** `AllViewer` (with Host) would send the CloudFront domain as the Host header: the ALB's host rule wouldn't match (rule 6 is host-only too, so it would fall through to the shared platform's default action) and the A3 certificate wouldn't match the name. `AllViewerExceptHostHeader` lets CloudFront send the origin's own name, and still forwards `Authorization`.
- **`EventSource` can't send a token**, so the SPA reads the stream with `fetch` (`web/src/lib/api.ts`, `streamChat`). A browser tool that "replays" the request without the header gets 401.
- **Health checks bypass listener rules**, so `/healthz` needs no secret header; anything else on the task does, through rule 4.
- **The MCP container has no port mapping on purpose.** Both containers share the task's network namespace, so the agent reaches `127.0.0.1:8081`; nothing outside the task can.
- **Quota warning or block "never shows":** `GET /acp/quota` returns `used: null` when the agent can't reach the cache; the task log has `cache unavailable` with the reason (same diagnosis as `services/api/README.md` → Console setup step 4).
- **A tool chip says `error` but the `mcp` stream has no `tool call` line:** the MCP SDK rejected the arguments before the tool ran. The agent's `tool result` line has `args`, the argument names the model sent (not their values); compare them with the tool's inputs in `services/mcp-server/README.md`. That's how issue #33 (`projectId` sent, `project_id` expected) was found.
- **Unproven permissions**: `bedrock:InvokeModel` from `ecs-task-role`, DynamoDB `DeleteItem`/`BatchWriteItem` for chat delete, and the CloudFront origin to the ALB. The first `make check-chat` settles them; a denial shows as an `error` event (Bedrock) or a 500 with the AWS error in the task log.
