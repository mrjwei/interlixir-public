# services/stream-router

Lambda **outside the VPC**, on the DynamoDB table stream (`NEW_AND_OLD_IMAGES`). Implements change data capture: an event exists if and only if the table write committed.

| File | What |
| --- | --- |
| `handler.py` | `to_event(record)` turns one stream record into `(detail-type, detail)`; `handler` puts them on the bus in batches of 10 |
| `filters.json` | The event source mapping's filter criteria, in the shape `--filter-criteria` and the console take |
| `tests/test_handler.py` | Unit tests, including the filters against sample records: `make test-stream-router` |

**Routing** (fields per `services/shared/README.md`):
- REMOVE with `SK` prefix `PROJECT#` → `DeleteRequested` (`entity = Project`; `userId` from `PK`)
- REMOVE with `SK` prefix `DOC#` → `DeleteRequested` (`entity = Document`; `userId` from the item's `userId` attribute; also `jobId` and `recordingId` from the item when it has them, so GC can remove the job and the recording the API deleted with it). A Document without `userId` is logged and skipped: GC couldn't find its S3 object, and a retry won't add the attribute
- MODIFY of a `JOB#` item, old stage `Failed`, new stage `Queued` → `RetryRequested`

Everything else is dropped by the filters before the function runs. `to_event` checks the same conditions again, so a filter edit can't make it emit the wrong event.

**Retries and duplicates:**
- `eventId` = the stream record's `eventID`, and `occurredAt` = its `ApproximateCreationDateTime` (second precision). A retried batch puts identical events, and consumers dedupe on `eventId`.
- A partial `PutEvents` failure raises, so the mapping retries the batch: bisect on error, 3 attempts, then the batch's shard and sequence numbers go to `interlixir-stream-router-dlq`.

**No table reads.** GC's own deletes come back as more REMOVE records (a project delete also emits one `DeleteRequested` per Document). GC treats a delete for something already gone as a no-op (`services/gc/`). Renames and edits (Project and Document MODIFY) are dropped by the filters. Checking "does the project still exist" here would need a read per record and a second IAM surface.

**Deploy:** `make zip-stream-router`, then upload `dist/stream-router.zip` to the `interlixir-stream-router` Lambda.
