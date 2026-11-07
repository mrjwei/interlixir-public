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

## Console settings

| Setting | Value |
| --- | --- |
| Code | `make zip-dlq-handler` → `dist/dlq-handler.zip`. Upload in the console (Code → Upload from → .zip file) |
| Function | `interlixir-dlq-handler`, **Python 3.14**, **arm64**, handler **`handler.handler`** (set it **before** adding the triggers, see M8's pitfall), role `lambda-role`, no VPC, both tags |
| Memory / timeout | 128 MB / **30 s** (= the DLQs' 30 s visibility timeout; Lambda refuses an SQS trigger whose queue visibility is shorter than the function timeout). Clients: 3 s connect, 5 s read, 3 attempts |
| Environment | none (code defaults: table, uploads bucket, bus) |
| Triggers | **Three** SQS triggers: `interlixir-upload-dlq`, `interlixir-transcription-dlq`, `interlixir-synthesis-dlq`. Batch size 10, batching window 0, **Report batch item failures** on, both tags on each mapping. Leave the DLQs' own settings as they are (no redrive on a DLQ) |

**DLQ alarm** (the M11 evidence "a DLQ alarm firing"; M16 adds it to the dashboard). CloudWatch → Alarms → Create alarm → Select metric → **Add math**:

| Setting | Value |
| --- | --- |
| Metrics | `m1`, `m2`, `m3` = `AWS/SQS` `NumberOfMessagesReceived`, `QueueName` = the three DLQs above, statistic **Sum**, period **1 minute** |
| Expression | `e1` = `FILL(m1, 0) + FILL(m2, 0) + FILL(m3, 0)` (a quiet queue publishes nothing; `FILL` stops the sum from going missing), label "Dead letters received". Alarm on `e1` only |
| Condition | **Greater than or equal to 1**, 1 out of 1 datapoints, missing data **not breaching** |
| Actions | none (SNS isn't available, the design): the state change is the evidence (alarm History tab) |
| Name / tags | `interlixir-dead-letters`, both tags |

Why `NumberOfMessagesReceived` and not the queue depth: the handler takes a dead letter within seconds, so `ApproximateNumberOfMessagesVisible` (sampled once a minute) would often read 0. Every dead letter is received at least once by the handler, so the received count can't miss one.

Read-only checks after creating it:
- `aws lambda get-function-configuration --function-name interlixir-dlq-handler --query '{h:Handler,t:Timeout,m:MemorySize,a:Architectures,r:Role}'`
- `aws lambda list-event-source-mappings --function-name interlixir-dlq-handler --query 'EventSourceMappings[].{q:EventSourceArn,s:State,b:BatchSize,f:FunctionResponseTypes}'`
- `aws cloudwatch describe-alarms --alarm-names interlixir-dead-letters --query 'MetricAlarms[].{s:StateValue,m:Metrics[].Id}'`
- `aws lambda get-function --function-name interlixir-dlq-handler --query Tags` (check the keys have no stray spaces)

**Deployed 2026-10-18** as above (read back in the build logs). The M8 job whose commit was denied (`ConditionCheckItem`) had already been redriven to `Done` with the fixed synthesis, so all three DLQs were empty when the triggers went on, and the handler hasn't run yet (no log group). Its first run is `make check-failures`' `forced` scenario, which also gives the "retry succeeding" evidence.

Milestone: **M11**.
