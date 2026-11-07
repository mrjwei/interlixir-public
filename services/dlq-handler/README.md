# services/dlq-handler

Dead-letter handler. **Zip** Lambda (Python 3.14, arm64), **outside the VPC**, with one SQS trigger on each pipeline dead-letter queue. When a message has used up its 3 receives, it marks the message's job `Failed` with a reason the user can read and puts `JobFailed` on the bus. The user's **Retry** then resumes the job from what it already has, so no job is left waiting in a stage that will never move.

| File | What |
| --- | --- |
| `handler.py` | Which job a dead letter belongs to (per queue), the stale checks, `JobFailed` + the conditional `Failed` write |
| `tests/` | `make test-dlq-handler` (moto, 19 tests). One-time setup: `make venv-dlq-handler` |

## What each dead-letter queue holds

| Queue | Message | The job must still be | Reason the user sees |
| --- | --- | --- | --- |
| `interlixir-upload-dlq` | S3 notification for `recordings/…` (the Recording item gives the `jobId`), or a `RetryRequested` event | `Queued`, at `retryCount` 0 (notification) or the event's `retryCount` | "The recording couldn't be prepared for transcription after several attempts. Retry the job; if it fails again, upload the file again." |
| `interlixir-transcription-dlq` | `TranscriptionChunk` | `Transcribing`, with the message's `chunksTotal` (otherwise it's from an older chunking) | "Part *n* of *N* of the recording couldn't be transcribed after several attempts. Retry the job: only the parts that are missing are transcribed again." |
| `interlixir-synthesis-dlq` | `SynthesisRequest` | `Transcribing`, `Redacting` or `Synthesising` | "The document couldn't be written after several attempts (the AI model may have been busy or unavailable). Retry the job: the steps that already finished aren't repeated." |

- **Stale messages change nothing.** The job is read first; the write is conditional on that stage, user and `retryCount`. A job that is `Done`, already `Failed`, retried or deleted is left alone.
- **`JobFailed` goes out before the write** (`eventId` = `jobfailed-<jobId>-<retryCount>`, `stage` = where it was), as the job creator and synthesis do: a crash between the two re-sends the same event rather than losing it. The history writer dedupes on `eventId`.
- **The message is not kept.** A handled record is deleted from its DLQ. A record that can't be tied to a job (not JSON, the wrong shape, the job is gone) is **logged in full** (`dropped: …`, body up to 4,000 chars) and dropped. An AWS error leaves the record on the DLQ and it is tried again after the queue's 30 s visibility timeout.
- `doneChunks` and the saved steps are left as they are: the retry reconciles them with S3.

## Deployment

| Setting | Value |
| --- | --- |
| Function | `interlixir-dlq-handler`, Python 3.14, arm64, handler `handler.handler`, no VPC, no environment. Build with `make zip-dlq-handler` |
| Memory / timeout | 128 MB / 30 s (Lambda refuses an SQS trigger whose queue visibility timeout is shorter than the function timeout, and the DLQs use 30 s) |
| Triggers | Three SQS triggers: `interlixir-upload-dlq`, `interlixir-transcription-dlq`, `interlixir-synthesis-dlq`. Batch size 10, report batch item failures on |

**Alarm.** `interlixir-dead-letters` alarms when `e1 = FILL(m1, 0) + FILL(m2, 0) + FILL(m3, 0) >= 1`, where `m1`–`m3` are `NumberOfMessagesReceived` (Sum, 1 minute) on the three DLQs, and missing data is not breaching. It uses `NumberOfMessagesReceived` rather than queue depth because the handler drains a dead letter within seconds, so a depth sample taken once a minute would often read 0, while every dead letter is received at least once.
