# services/mcp-server

The MCP tool server, **internal only**: a sidecar container (`mcp`) in the chat-agent task, listening on `127.0.0.1:8081`, with no port mapping and no ALB route (G10; Service Connect isn't on the unit's list). Python, the official MCP SDK 2.x (`MCPServer`), Streamable HTTP in **stateless JSON** mode: every `POST /mcp` is complete on its own. Milestone **M18**.

**`userId` comes only from the verified token, never from tool arguments.** The agent forwards the user's Cognito ID token as `Authorization: Bearer …` on every MCP request; every tool call verifies it (`auth.py`, the same checks as the API) and builds every key from its `sub`. No tool has a user argument (a test checks the published schemas). The server is read-only: it has no write path at all.

## Tools

All read-only (`readOnlyHint`). A project or document that is missing or someone else's is the same tool error ("No such … for this user"), as is an ID with a key delimiter.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_projects` | none | `projectId`, `name`, `createdAt`, `lastDocumentAt` per project |
| `list_documents` | `projectId` | `projectName`; `documents` (`documentId`, `title`, `schemaId`, `createdAt`); `notReady`: recordings without a document, with their job's `stage` (and `failureReason`) |
| `list_documents` (no `projectId`) | none | every project's `documents` in one call, newest first, each with `projectId` and `projectName` (at most 100, then `truncated`), `count`, and `notReady` with `projectName`. Discovery in one round instead of one per project (issue #30) |
| `read_document` | `projectId`, `documentId` | `title`, `schemaId`, `createdAt`, `markdown` (cut at 32,000 characters, with `truncated` saying so) |
| `get_history` | `limit` (1–50, default 10) | The caller's events newest first: `eventType`, `occurredAt`, `projectId`, `fileName`, `jobId` or `chatSessionId`, `stage`, `tokenCount`, `cost` |
| `get_job_stats` | none | Per project: `jobsByStage`, `audioMinutes`, `tokenCount`, `cost` (up to 100 jobs per project, through GSI1) |

Documents are nested under their project (`PROJECT#…` / `DOC#…`, as in the API), so `read_document` takes the project too; `list_documents` gives both IDs. The arguments have the same names as the fields in the list results, because the model copies the keys as well as the values: with `project_id` / `document_id` it sent `projectId` / `documentId` and every read failed validation (issue #33). Ownership is checked as in `services/api/app/store.py`: projects by key, documents and jobs by their `userId` attribute, a project's contents after the project lookup, and the document's S3 key built from the caller's `userId`.

Every call logs one JSON line, `tool call`, with `Tool`, `userId`, `outcome` (`ok`, `not found`, `unauthorized`, `error`) and `durationMs`, which is also the `ToolLatency` metric by `Tool`. Arguments and results aren't logged: they are the user's research content.

## Layout

| File | What |
| --- | --- |
| `mcp_server/server.py` | The tools, the per-call token check (`_call`), `/healthz`, the Streamable HTTP app |
| `mcp_server/auth.py` | `user_id(authorization)`: the verified `sub`, or `Unauthorized` |
| `mcp_server/store.py` | Read-only DynamoDB and S3, with the ownership checks |
| `tests/test_server.py` | Every tool through the real MCP client over HTTP (uvicorn in a thread), with real tokens, against moto: `make venv-chat`, then `make test-mcp-server` |
| `tests/fixtures.py` | Two users' seeded data and the token key; the chat agent's tests use it too |
| `Dockerfile` | `python:3.14-slim`, `nobody`, `uvicorn mcp_server.server:app --host 127.0.0.1 --port 8081 --no-access-log` |

Config: `TABLE_NAME`, `DOCS_BUCKET`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `AWS_REGION`, all with the real values as defaults, so the container needs no environment. It runs as the task role (`ecs-task-role`): DynamoDB `GetItem`/`Query` and S3 `GetObject` on the docs bucket, all proven for that role by the API. Deployment: `services/chat-agent/README.md` → Console setup.
