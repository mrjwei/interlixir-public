# services/history-writer

History writer. **Zip** Lambda (Python 3.14, arm64), **outside the VPC**. A rule on the bus sends the five history events to an SQS queue, and this function writes one **Event item** per event in the user's partition. `GET /a3/history` pages through those items newest first (5, then 10 per cursor), and the SPA's History panel shows them.

| File | What |
| --- | --- |
| `handler.py` | Checks the event, claims its `eventId`, adds the job's project and file name, writes the Event item |
| `tests/` | `make test-history-writer` (moto, 13 tests; inputs are the shared contract's examples). One-time setup: `make venv-history-writer` |

Path: producer `PutEvents` → bus `interlixir-bus` → rule `interlixir-history` → SQS `interlixir-history` → this function → table.

## What it writes

| Item | PK | SK | Attributes |
| --- | --- | --- | --- |
| Event | `USER#<userId>` | `EVENT#<occurredAt>#<eventId>` | `eventType`; `jobId` or `chatSessionId`; `tokenCount`, `cost` when the event has them; `stage` (`JobFailed`: where it failed); `projectId`, `fileName` (job events, copied from the Job and Recording items) |
| Event claim | `USER#<userId>` | `EVENTID#<eventId>` | `eventSk` (the Event item's SK it belongs to), `expiresAt` (30 days, the table's TTL) |

- **Why a claim.** Producers stamp `occurredAt` when they publish, so a producer that crashed after publishing sends the same `eventId` again with a later `occurredAt`, which gives a different Event key. The claim is keyed by `eventId` alone. Its condition (`attribute_not_exists(PK) OR eventSk = :sk`) lets the same publish through again (a redelivery rewrites the same item and finishes a write that was cut off) and stops a re-publish. The user sees each event once, at its first time.
- **`EVENTID#` doesn't collide with `EVENT#`.** History queries `begins_with(SK, "EVENT#")`, and `EVENTID#…` doesn't match that prefix (an API test covers it).
- **Both writes are `UpdateItem`.** An update creates the item. `PutItem` is unproven on `lambda-role`, and with this order no transaction is needed.
- **Context for the panel:** one `GetItem` on the Job (its `GSI1PK` gives the project) and one on the Recording (`fileName`), made only for the first delivery. A job that is gone (project deleted) or belongs to someone else gets no context. The row is still written.
- **Dropped, not retried:** anything that isn't one of the five history types from `interlixir.app` with valid IDs, a fixed-width `occurredAt`, an owner ID, `tokenCount`/`cost` where the contract requires them, and a valid `JobFailed` stage. It's logged in full (`dropped: not a history event`, up to 4,000 chars). **Retried:** any AWS error. The record is returned as a batch item failure, SQS redelivers it, and after 3 receives it moves to `interlixir-history-dlq`.
- The API caches history pages for 30 s and the function can't reach the cache. A new event shows on a cached page once the TTL ends.

Log lines (JSON): `event recorded`, `skipped: duplicate eventId`, `dropped: not a history event`, `event not recorded, will be redelivered` (with traceback). Each carries `messageId`, `eventId`, `eventType`, `occurredAt` and `jobId` where it has them.

## Console settings

Create them in this order. The rule goes last, so the first event it matches already has somewhere to go.

**1. Queues** (SQS → Create queue, Standard, SSE-SQS left on, both tags):

| Queue | Visibility timeout | Retention | Receive wait | Dead-letter queue | Access policy |
| --- | --- | --- | --- | --- | --- |
| `interlixir-history-dlq` | 30 s (default) | **14 days** | 0 | – | default (owner only) |
| `interlixir-history` | **180 s** (6 × the function's 30 s timeout, AWS's guidance for SQS triggers) | 4 days | 0 (Lambda polls) | **`interlixir-history-dlq`, maximum receives 3** | default (owner only): the rule delivers through a role, not a queue policy |

**2. Function:**

| Setting | Value |
| --- | --- |
| Code | `make zip-history-writer` → `dist/history-writer.zip`. Upload it in the console (Code → Upload from → .zip file) |
| Function | `interlixir-history-writer`, **Python 3.14**, **arm64**, handler **`handler.handler`** (set it **before** adding the trigger; see M8's pitfall), role `lambda-role`, no VPC, both tags |
| Memory / timeout | 128 MB / **30 s**. Clients: 3 s connect, 5 s read, 3 attempts |
| Environment | none (code default: table `interlixir-data`) |
| Trigger | SQS `interlixir-history`: batch size 10, batching window 0, **Report batch item failures** on, both tags on the mapping |

**3. Rule** (EventBridge → Rules → Create rule, event bus **`interlixir-bus`**):

| Setting | Value |
| --- | --- |
| Name | `interlixir-history`, rule type "Rule with an event pattern", both tags |
| Event pattern (custom) | `{"source": ["interlixir.app"], "detail-type": ["UploadReceived", "TranscriptionDone", "SynthesisDone", "JobFailed", "ChatSession"]}` |
| Target | SQS queue `interlixir-history`. Execution role: **use existing**, `eventbridge-rules-role` (as the tap and the `RetryRequested` rule do). Leave the target's retry policy and DLQ settings at their defaults |

Check the pattern before saving: `aws events test-event-pattern --event-pattern file://<pattern>.json --event "$(cat services/shared/contracts/examples/SynthesisDone.json)"` → `true`, and `false` for `examples/RetryRequested.json`.

**Read-only checks after creating them:**
- `aws lambda get-function-configuration --function-name interlixir-history-writer --query '{h:Handler,t:Timeout,m:MemorySize,a:Architectures,r:Role}'`
- `aws lambda list-event-source-mappings --function-name interlixir-history-writer --query 'EventSourceMappings[].{q:EventSourceArn,s:State,b:BatchSize,f:FunctionResponseTypes}'`
- `aws sqs get-queue-attributes --queue-url $(aws sqs get-queue-url --queue-name interlixir-history --query QueueUrl --output text) --attribute-names VisibilityTimeout RedrivePolicy`
- `aws events list-targets-by-rule --event-bus-name interlixir-bus --rule interlixir-history --query 'Targets[].{a:Arn,r:RoleArn}'`
- `aws lambda get-function --function-name interlixir-history-writer --query Tags` (check the keys have no stray spaces)

**Then:** `make check-history ARGS="--job-id <a job that isn't Done yet>"` or `ARGS="--speech"` (the Whisper worker needs desired count 1). Events published before the rule existed aren't in anyone's history: the bus doesn't keep them. A new job is needed for the check.

**Pitfalls:**
- A rule on the **default** bus never sees these events. Pick `interlixir-bus` in the bus selector before creating the rule.
- If the console offers to "create a new role for this specific resource", don't: role creation is denied (§3.3). Pick the existing unit role.
- The history DLQ isn't a pipeline DLQ: **don't** add it to the dead-letter handler's triggers (it would try to read events as job messages and drop them). M16 adds it to the dead-letter alarm. Redrive it from the console (SQS → `interlixir-history-dlq` → Start DLQ redrive) once the cause is fixed. Re-running is safe, because the writer is idempotent.

Milestone: **M13**.
