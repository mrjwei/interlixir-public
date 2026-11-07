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

## Deployment

Create the queues and function first and the rule last, so the first event it matches already has somewhere to go.

| Resource | Settings |
| --- | --- |
| Queues | `interlixir-history-dlq` (14-day retention) and `interlixir-history` (visibility 180 s = 6 × the function timeout, redrive to the DLQ after 3 receives) |
| Function | `interlixir-history-writer`, Python 3.14, arm64, handler `handler.handler`, 128 MB / 30 s, no VPC, no environment. Build with `make zip-history-writer` |
| Trigger | SQS `interlixir-history`, batch size 10, report batch item failures on |
| Rule | `interlixir-history` on **`interlixir-bus`**, pattern `{"source": ["interlixir.app"], "detail-type": ["UploadReceived", "TranscriptionDone", "SynthesisDone", "JobFailed", "ChatSession"]}`, target SQS `interlixir-history` |

Notes:
- A rule on the default bus never sees these events; pick `interlixir-bus`.
- The bus keeps no events, so anything published before the rule existed isn't in anyone's history.
- The history DLQ isn't a pipeline DLQ: don't add it to the dead-letter handler's triggers (it would try to read events as job messages and drop them). Redrive it from SQS once the cause is fixed; re-running is safe because the writer is idempotent.
