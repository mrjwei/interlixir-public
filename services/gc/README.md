# services/gc

Garbage collector. **Zip** Lambda (Python 3.14, arm64), **outside the VPC**. Nothing in S3 expires (lifecycle rules are denied, the account's permission limits), so GC is the only thing that removes a deleted project's raw audio, chunks, transcripts, context files and documents.

| File | What |
| --- | --- |
| `handler.py` | Checks the `DeleteRequested` event and the owner, then removes what belonged to the Project or Document |
| `tests/` | `make test-gc` (moto, 13 tests: no orphans, re-runs and cascades are no-ops, another user's things are left, bad events dropped). One-time setup: `make venv-gc` |

Path: the API deletes an item → table stream → stream router (`DeleteRequested`) → bus `interlixir-bus` → rule `interlixir-delete-requested` → SQS `interlixir-gc` → this function → S3 and the table.

## What the API deletes, and what GC removes

| User action | API (synchronous, then 204) | GC (asynchronous, seconds later) |
| --- | --- | --- |
| Delete project | The Project item (`USER#<userId>` / `PROJECT#<projectId>`). The sidebar drops it at once | Every job of the project (`GSI1PK = PROJECT#<projectId>`): `chunks/<jobId>/`, `transcripts/<userId>/<jobId>/`, its UsesAsContext links, then the Job item. Then `recordings/<userId>/<projectId>/` and `context/<userId>/<projectId>/` (files and their extracted `.txt`), then every item in `PROJECT#<projectId>`, each Document's `.md` first |
| Delete document | The Document **and its Recording** in one transaction, so the sidebar (which lists recordings) drops the entry at once | The `.md`; `recordings/<userId>/<projectId>/<recordingId>.*`; the job's context files **if no other job of the project links to them** (a context file is shared by every recording of one upload); the job's chunks, transcripts and links, then the Job item |

- **Removing the Documents of a deleted project** comes back through the stream as one `DeleteRequested` per Document. Those runs find nothing left and change nothing.
- **Idempotent.** Each step deletes what is there and skips what isn't. The Job item goes last because a re-run finds the job's links through it. A `DeleteRequested` for a Project or Document that still exists (stale or replayed) is skipped.
- **Ownership.** Prefixes are built from the event's `userId`, which the stream router took from the removed item's key or `userId` (never from a request). Before deleting, GC checks that a Job's `userId` is the event's (and, for a document, that its `GSI1PK` is the project), a Document's `userId`, and a Recording's or ContextFile's `storageKey` prefix. Anything else is logged (`skipped: … of another user`) and left.
- **Docs bucket by key only.** Listing needs `s3:ListBucket`, which the role is known to have only on the uploads bucket, so documents are deleted by their exact key.
- **Known gap.** A Whisper worker or a synthesis run that already holds a job when it's deleted can still write one transcript or step file after GC has run (both stop at the missing Job item, M7/M8). At most that file is left behind under `transcripts/<userId>/<jobId>/`.
- **Not removed:** the user's history rows about the project's jobs (their activity log; a link to a deleted job shows "not found") and chat sessions.

Log lines (JSON): `collected` (`entity`, `projectId`, `documentId`, `objects` found by prefix and deleted, `items` that were still there, `skipped`, `durationMs`), `skipped: project still exists` / `document still exists`, `skipped: … of another user`, `dropped: not a DeleteRequested event` (body up to 4,000 chars), `delete not finished, will be redelivered` (with traceback).

## Console setup

**Done 2026-10-27** with the CLI, these settings, in this order; every command and read-back is in the build logs, and the resources are in the deployment notes (GC). Kept as the recipe for a rebuild.

Create them in this order: GC first, then the API and SPA that produce deletes. The bus keeps no events, so a delete made before the rule exists is never collected.

**1. Queues** (SQS → Create queue, Standard, SSE-SQS left on, both tags):

| Queue | Visibility timeout | Retention | Receive wait | Dead-letter queue | Access policy |
| --- | --- | --- | --- | --- | --- |
| `interlixir-gc-dlq` | 30 s (default) | **14 days** | 0 | – | default (owner only) |
| `interlixir-gc` | **720 s** (6 × the function's 120 s timeout) | 4 days | 0 (Lambda polls) | **`interlixir-gc-dlq`, maximum receives 3** | default (owner only): the rule delivers through a role |

**2. Function** (Lambda → Create function → Author from scratch):

| Setting | Value |
| --- | --- |
| Code | `make zip-gc` → `dist/gc.zip`. Upload it (Code → Upload from → .zip file) |
| Function | `interlixir-gc`, **Python 3.14**, **arm64**, execution role **use an existing role** `lambda-role`, no VPC, both tags. Runtime settings → handler **`handler.handler`** (set it **before** adding the trigger, M8's pitfall) |
| Memory / timeout | 128 MB / **120 s** (Configuration → General configuration). Clients: 3 s connect, 10 s read, 3 attempts |
| Environment | none (code defaults: table `interlixir-data`, buckets `interlixir-uploads` and `interlixir-docs`) |
| Trigger | SQS `interlixir-gc`: **batch size 1**, batching window 0, **Report batch item failures** on, both tags on the mapping. One delete per invocation: a project can hold many jobs |

**3. Rule** (EventBridge → Rules → Create rule, event bus **`interlixir-bus`**):

| Setting | Value |
| --- | --- |
| Name | `interlixir-delete-requested`, rule type "Rule with an event pattern", both tags |
| Event pattern (custom) | `{"source": ["interlixir.app"], "detail-type": ["DeleteRequested"]}` |
| Target | SQS queue `interlixir-gc`. Execution role: **use existing**, `eventbridge-rules-role`. Leave the target's retry policy and DLQ at their defaults |

Check the pattern first: `aws events test-event-pattern --event-pattern '{"source":["interlixir.app"],"detail-type":["DeleteRequested"]}' --event "$(cat services/shared/contracts/examples/DeleteRequested.project.json)"` → `true`; with `examples/RetryRequested.json` → `false`.

**4. Redeploy the stream router** (optional but recommended): `make zip-stream-router`, then Lambda → `interlixir-stream-router` → Code → Upload from → .zip file. Its Document events now also name `jobId` and `recordingId`. Without the redeploy GC still works (it falls back to `documentId` = `jobId` and reads the recording from the Job). **The mapping's filter criteria don't change**: they already pass Project and Document REMOVEs and drop the MODIFYs a rename or edit makes (read back 2026-10-27).

**5. Redeploy the API** with the M20 routes: `make push-api`, then ECS → Task definitions → `interlixir-api` → the live revision (6) → **Create new revision**, change only the `api` container's image to the pushed digest, keep everything else (0.25 vCPU / 0.5 GB, `CACHE_ENDPOINT`, `CACHE_TLS=true`, both tags). ECS → `interlixir-api` → **Update service** → the new revision → wait for the rollout. Nothing changes on the auth service's route (`ANY /a3/{proxy+}`), the shared ALB rule (path `/a3/*`) or CloudFront's `/a3/*` behaviour (all methods since M9), so `PATCH` and `DELETE` already reach the service.

**6. Alarm and dashboard** (CLI, the account's permission limits: the console's alarm form drops the tags): `aws cloudwatch put-metric-alarm --cli-input-json file://docs/observability/alarms/dead-letters.json` (adds `-gc-dlq` as `m5`) and `aws cloudwatch put-dashboard --dashboard-name interlixir --dashboard-body file://docs/observability/dashboard.json` (the DLQ depth widget gains `gc`).

**7. SPA:** `CLOUDFRONT_DISTRIBUTION_ID=<distribution-id> make deploy-web`.

**Read-only checks after creating them:**
- `aws lambda get-function-configuration --function-name interlixir-gc --query '{h:Handler,t:Timeout,m:MemorySize,a:Architectures,r:Role,rt:Runtime}'`
- `aws lambda list-event-source-mappings --function-name interlixir-gc --query 'EventSourceMappings[].{q:EventSourceArn,s:State,b:BatchSize,f:FunctionResponseTypes}'` → `Enabled`, 1, `ReportBatchItemFailures`
- `aws sqs get-queue-attributes --queue-url $(aws sqs get-queue-url --queue-name interlixir-gc --query QueueUrl --output text) --attribute-names VisibilityTimeout RedrivePolicy`
- `aws events list-targets-by-rule --event-bus-name interlixir-bus --rule interlixir-delete-requested --query 'Targets[].{a:Arn,r:RoleArn}'`
- `aws lambda get-function --function-name interlixir-gc --query Tags` (no stray spaces in the keys; synthesis once had `" purpose"`)
- `aws ecs describe-services --cluster interlixir-ecs-cluster --services interlixir-api --query 'services[0].{td:taskDefinition,r:deployments[0].rolloutState}'`

**Then:** `make check-gc 2>&1 | tee the build logs` (needs `APP_USER`/`APP_PASSWORD`; the Whisper worker scales out from 0, so allow 5–10 minutes). It prints the inventory before, after the document delete and after the project delete, and passes only if the last column is all zero.

**Pitfalls:**
- A rule on the **default** bus never sees these events: pick `interlixir-bus` in the selector.
- Don't let the console "create a new role" for the function or the rule (role creation is denied, §3.3).
- The GC DLQ isn't a pipeline DLQ: **don't** add it to the dead-letter handler's triggers. After fixing the cause, redrive it (SQS → `interlixir-gc-dlq` → Start DLQ redrive); GC is idempotent, so a redrive is safe.
- If `check-gc` shows GC's log with `AccessDenied`, record it in the account's permission limits and use its fallback; don't look for another permission path (§0.7).

Milestone: **M20**.
