# services/api

One container service (`interlixir-api`, FastAPI on port 8080) on ECS Fargate in the private subnets, serving every `/app` route, plus `/healthz` for the ALB. Path: the auth gateway's HTTP API (JWT authorizer) → `ANY /a3/{proxy+}` HTTP proxy with a secret header → the shared ALB → task.

**Rules (review checklist)**
- Verify the Cognito ID token in the service too (JWKS, issuer, `token_use = id`, `aud` = app client). `userId` comes **only** from the verified token claims (`sub`), and every DynamoDB key is built from it. Return **404, not 403**, for other users' IDs.
- Talks only to DynamoDB (through the NAT), S3 (gateway endpoint) and Memcached. **Never call SQS, EventBridge or Bedrock**. Side effects go through table writes picked up by `stream-router`.
- Retry = conditional update `Failed → Queued`, `retryCount + 1`. Delete = one conditional item delete (a document's with its Recording, in one transaction); GC removes the rest from the stream. Rename and edit = conditional updates, which the stream's filters ignore.
- Presign = pre-signed **POST** policies, and pre-creates the Recording, Job and `UsesAsContext` items.
- Cached reads return `X-App-Cache: HIT|MISS|BYPASS|OFF`. Every write deletes the cache keys it affects, after it commits. Only owner-checked responses are cached, under the caller's userId; never a 404.

## Layout

| File | What |
| --- | --- |
| `app/main.py` | The app: routers, `/healthz`, `NotFound` → 404 and `Conflict` → 409 |
| `app/auth.py` | `UserId`, the only source of the caller's ID: the `sub` of a verified Cognito ID token (JWKS cached 6 h, RS256 only, issuer, `aud`, `token_use = id`, `exp`). 401 on any failure, 503 if the JWKS can't be fetched |
| `app/keys.py` | Single-table key builders, `ID_PATTERN`, `new_id()` (UUIDv7) |
| `app/store.py` | Every DynamoDB/S3 call. Ownership checks live here, and each write's cache invalidation |
| `app/cache.py` | Memcached: `aside()` (cache-aside on a route's response, sets `X-App-Cache`, logs the EMF line), `invalidate()`, the key builders and TTLs, and `count_in_window()` (hourly quota counters for the chat agent). Fails open, with a 10 s backoff after an error |
| `app/routes/` | One router per resource; they shape responses and nothing else. `uploads.py` also holds the upload limits and the extension → Content-Type table |
| `tests/test_api.py` | Route tests against moto: `make venv-api` once, then `make test-api` |
| `tests/test_uploads.py` | Presign route: items pre-created in one transaction, each policy's conditions decoded and checked, every rejection writes nothing |
| `tests/test_auth.py` | Token verification against a local RSA key served as the JWKS |
| `tests/test_cache.py` | Cache-aside, invalidation, per-user keys, fail-open, the EMF line and the quota window, against an in-memory fake with Memcached's semantics; `TestRealMemcached` repeats the calls against a real server when `MEMCACHED_TEST_ENDPOINT` is set |
| `Dockerfile` | `python:3.14-slim`, runs as `nobody`, `uvicorn` on 8080. Build for `linux/arm64` |

## Routes

All `/app` routes need a token. IDs must match `^[A-Za-z0-9_-]{1,128}$` (422 otherwise), so no ID can carry a key delimiter (`#`, `/`).

| Method and path | Does |
| --- | --- |
| `GET /healthz` | `{"status":"ok"}`. Liveness only, no dependency calls |
| `GET /a3/projects` | The caller's projects (sidebar). Cached 10 s |
| `POST /a3/projects` | Create, body `{"name"}` (1–100 chars, trimmed) → 201. Invalidates the list |
| `GET /a3/projects/{projectId}` | The project plus its recordings (with `jobId`) and documents. Cached 10 s |
| `PATCH /a3/projects/{projectId}` | Rename, body `{"name"}` (as for create) → the project. 404 if it isn't the caller's (the update can't create one). Invalidates the list and the project |
| `DELETE /a3/projects/{projectId}` | Delete the Project item → 204 (404 if it isn't the caller's). GC removes its recordings, jobs, documents and files. Invalidates the list and the project |
| `POST /a3/projects/{projectId}/uploads` | Presign (below) → 201 with a POST policy per file. 404 if it isn't the caller's project; 422 for anything outside the limits or an unknown schema. Invalidates the project's contents |
| `GET /a3/projects/{projectId}/documents/{documentId}` | Title, `jobId`, the Markdown from the docs bucket, and `contextFiles` (`contextFileId`, `fileName`) the job used, from its UsesAsContext links |
| `PATCH /a3/projects/{projectId}/documents/{documentId}` | Body `{"title"?, "markdown"?}`, at least one (title 1–200 chars, trimmed; Markdown up to 500,000 chars) → `{documentId, projectId, title, updatedAt}`. Markdown overwrites the object (versioning is off). A title on its own also rewrites the Markdown's first line if it is still the generated `# <old title>` (best effort: a failed write is logged, the rename stands). Invalidates the project (titles) |
| `DELETE /a3/projects/{projectId}/documents/{documentId}` | The Document **and its Recording** in one transaction → 204, so the sidebar drops the entry at once. The stream router's `DeleteRequested` names the job and recording; GC removes the job, audio, chunks, transcripts, `.md` and any context file no other job uses. Invalidates the project and the job |
| `GET /a3/projects/{projectId}/documents/{documentId}/export` | `{url, fileName, expiresIn}`: a 300 s pre-signed GET with `Content-Disposition: attachment` |
| `GET /a3/jobs/{jobId}` | Stage, `chunksDone`/`chunksTotal`, `retryCount`, `failureReason`; plus `schemaId`, `instructionText`, `createdAt`, `audioSeconds`, and once Done `documentId`, `tokenCount`, `cost` (the SPA's progress and document views). Cached 5 s |
| `POST /a3/jobs/{jobId}/retry` | Conditional `Failed → Queued`, `retryCount + 1` → 202. 409 from any other stage. Invalidates the job's status |
| `GET /a3/schemas` | Built-ins plus the caller's own: `schemaId`, `name`, `description`, `builtIn` (no definitions) |
| `GET /a3/history?limit=5&cursor=…` | Events newest first. `limit` 1–50; `nextCursor` is the base64url `LastEvaluatedKey`, rejected (400) unless it's in the caller's partition. Each page cached 30 s, by limit and cursor. Events carry `eventType`, `occurredAt`, `jobId` or `chatSessionId`, and where the history writer stored them `tokenCount`, `cost`, `stage` (`JobFailed`), `projectId` and `fileName` (job events) |

**Uploads.** Body: `schemaId` (default `default`; built-in or the caller's own), `instructionText` (≤ 4000 chars), `recordings` (1–5 of `{fileName, contentType, sizeBytes}`; mp3, m4a, wav, webm, mp4; ≤ 200 MiB each) and `contextFiles` (0–5; pdf, docx, md, txt; ≤ 20 MiB in total). The declared `contentType` may be empty or `application/octet-stream`; otherwise it must be a known type for the extension. One DynamoDB transaction checks the project still exists and writes, per recording, a Recording (`jobId`, `storageKey`, `languageCode = en`) and a `Queued` Job (`schemaId`, `instructionText`, `userId`, GSI1 keys, `retryCount = 0`); per context file a ContextFile; and a UsesAsContext link from every job to every context file. Each policy (15 min) fixes the exact key, the extension's canonical Content-Type (returned in `fields`, so the client must send it) and `content-length-range` 1..**declared size**, so S3 also enforces the context total. The client posts `fields`, then `file` last, to `url` (regional, virtual-hosted). Response: `{expiresIn, recordings: [{recordingId, jobId, fileName, upload: {url, fields}}], contextFiles: [{contextFileId, fileName, upload}]}`.

**Documents are nested under their project.** A Document's key is `PROJECT#<projectId>` / `DOC#<documentId>`, so `/a3/documents/{id}` (the original plan) would need a new index or a lookup item. The client always knows the project from the sidebar.

**Ownership** (all in `store.py`): projects by key (`USER#<userId>`); jobs and documents by their `userId` attribute; project contents only after the project lookup succeeds. A document's S3 key is built from the caller's `userId`, not read from `storageKey`.

## Run

```bash
make venv-api && make test-api
cd services/api && PYTHONPATH=../shared .venv/bin/uvicorn app.main:app --port 8080   # /a3 routes need a real Cognito ID token
```

Config is environment variables with the real names as defaults: `TABLE_NAME` (`interlixir-data`), `DOCS_BUCKET` (`interlixir-docs`), `UPLOADS_BUCKET` (`interlixir-uploads`), `COGNITO_USER_POOL_ID` (`ap-southeast-2_EXAMPLE1`), `COGNITO_APP_CLIENT_ID` (`exampleclientid1234567890`), `AWS_REGION` (`ap-southeast-2`; Fargate sets it). None are secrets. **`CACHE_ENDPOINT`** (`host:port` of the Memcached configuration endpoint) has no default because its name is only known once the cluster exists; unset means no cache (every cached route says `OFF`), which is also how the tests and a local run work. **`CACHE_TLS`** (`true`/`false`, default `false`): TLS to the node, verifying its certificate; the task sets `true` because the node has in-transit encryption on.

## Cache

| Key | Route | TTL | Deleted by |
| --- | --- | --- | --- |
| `job:<userId>:<jobId>` | `GET /a3/jobs/{jobId}` | 5 s | retry; delete document |
| `projects:<userId>` | `GET /a3/projects` | 10 s | create, rename and delete project |
| `project:<userId>:<projectId>` | `GET /a3/projects/{projectId}` | 10 s | upload; rename and delete project; rename and delete document |
| `history:<userId>:<limit>:<sha256(cursor)>` | `GET /a3/history` | 30 s | nothing: only the history writer adds events (TTL only) |
| `quota:<userId>:<yyyymmddhh>` | (chat) | rest of the hour + 60 s | expires with its window |

- The pipeline's writes (stage changes, `doneChunks`, new documents) can't invalidate: they run in Lambdas and the worker, which can't reach the node. The TTLs bound that staleness.
- Header `X-App-Cache`: `HIT`, `MISS`, `BYPASS` (the client sent `Cache-Control: no-cache`: read DynamoDB, refresh the entry) or `OFF` (no endpoint, or skipped for 10 s after a cache error). Not `X-Cache`, which CloudFront sets on everything it serves.
- One log line per cached read, in Embedded Metric Format: namespace `interlixir`, metrics `CacheHit` and `CacheMiss` (Count), dimension `Route` (`job`, `projects`, `project`, `history`) plus a no-dimension total. `cache` in the same line is `hit`/`miss`/`bypass`/`off` for Logs Insights; a `cache unavailable, skipping it` warning names the failed operation.
- Values are compact JSON of the response body (Memcached's 1 MB item limit is far away). Keys over Memcached's 250 bytes become `<kind>:sha256:<hex>`.

### Deployment

- **ElastiCache Memcached**, "design your own cache", standard (not serverless): `interlixir-cache`, engine 1.6.x, port 11211, `cache.t4g.micro` × 1 node, the private subnet group, security group `CacheSG` only, **encryption in transit on** (TLS uses the same port; the client turns it on with `CACHE_TLS=true`).
- **API task definition:** the pushed image (`make push-api`), plus `CACHE_ENDPOINT` = `<configuration endpoint>:11211` and `CACHE_TLS=true`. Everything else is unchanged (0.25 vCPU / 0.5 GB, `AppSG`, private subnets).
- If every read says `OFF`, the task's log has `cache unavailable` with the cause: a timeout means the security-group path (task in `AppSG`, node in `CacheSG`); a name error means a typo in `CACHE_ENDPOINT`; `SSLCertVerificationError` means the endpoint isn't the name on the node's certificate (use the configuration endpoint as given); a connection reset at once means `CACHE_TLS` isn't `true`.
- **Hit ratio:** the `CacheHit` and `CacheMiss` metrics (namespace `interlixir`) appear after the first cached reads; graph `100 * m1 / (m1 + m2)` for the hit percentage.

Cost: the node bills by the hour whether used or not (the only always-on part besides the ALB and the API task). Put it in the Pricing Calculator, and delete it after marking: the API keeps working without it (`OFF`).
