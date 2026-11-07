# services/gc

Garbage collector. **Zip** Lambda (Python 3.14, arm64), **outside the VPC**. No S3 lifecycle rules are configured, so GC is the only thing that removes a deleted project's raw audio, chunks, transcripts, context files and documents.

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
- **Known gap.** A Whisper worker or a synthesis run that already holds a job when it's deleted can still write one transcript or step file after GC has run (both stop at the missing Job item). At most that file is left behind under `transcripts/<userId>/<jobId>/`.
- **Not removed:** the user's history rows about the project's jobs (their activity log; a link to a deleted job shows "not found") and chat sessions.

Log lines (JSON): `collected` (`entity`, `projectId`, `documentId`, `objects` found by prefix and deleted, `items` that were still there, `skipped`, `durationMs`), `skipped: project still exists` / `document still exists`, `skipped: … of another user`, `dropped: not a DeleteRequested event` (body up to 4,000 chars), `delete not finished, will be redelivered` (with traceback).

## Deployment

Create GC before the API and SPA routes that produce deletes: the bus keeps no events, so a delete made before the rule exists is never collected.

| Resource | Settings |
| --- | --- |
| Queues | `interlixir-gc-dlq` (14-day retention) and `interlixir-gc` (visibility 720 s = 6 × the function timeout, 4-day retention, redrive to the DLQ after 3 receives). Standard queues, SSE-SQS |
| Function | `interlixir-gc`, Python 3.14, arm64, handler `handler.handler`, 128 MB / 120 s, no VPC, no environment (code defaults for table and buckets). Build with `make zip-gc` |
| Trigger | SQS `interlixir-gc`, batch size 1 (one delete per invocation, since a project can hold many jobs), report batch item failures on |
| Rule | `interlixir-delete-requested` on **`interlixir-bus`** (not the default bus), pattern `{"source": ["interlixir.app"], "detail-type": ["DeleteRequested"]}`, target SQS `interlixir-gc` |

Notes:
- The GC DLQ isn't a pipeline DLQ: don't add it to the dead-letter handler's triggers. After fixing the cause, redrive it from SQS; GC is idempotent, so that's safe.
- The stream router must be at a version that puts `jobId` and `recordingId` on Document events (`make zip-stream-router`). Its filter criteria don't change.
- Test the rule's pattern with `aws events test-event-pattern` against `services/shared/contracts/examples/DeleteRequested.project.json` (matches) and `RetryRequested.json` (doesn't).
