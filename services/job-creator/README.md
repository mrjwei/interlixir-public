# services/job-creator

Chunker. Lambda **container image** (Python 3.14, arm64) with static ffmpeg/ffprobe 7.1.1, **outside the VPC**. It turns an uploaded recording into 5-minute chunks on the transcription queue, and resumes retried jobs.

| File | What |
| --- | --- |
| `handler.py` | Entry point: upload path (`start`), retry path (`resume`), the ordered writes and sends |
| `media.py` | `probe()` (readable? audio track? duration) and `split()` (one ffmpeg pass: first audio track → mono 16 kHz 16-bit WAV → `<n>.wav` every 300 s) |
| `Dockerfile` | `public.ecr.aws/lambda/python:3.14` + `mwader/static-ffmpeg:7.1.1` + the shared event contract (named build context `shared`) |
| `tests/` | `make test-job-creator` (moto; the ffmpeg tests need ffmpeg on PATH). One-time setup: `make venv-job-creator` |

## Triggers

Both arrive on the **upload queue** `interlixir-upload` through one event source mapping (batch size 1, `ReportBatchItemFailures`):

- **S3 notification** (`ObjectCreated:Post` on `recordings/`). Ignores `s3:TestEvent`, non-create events, other buckets, keys ending in `/` and size-0 objects; URL-decodes the key (`unquote_plus`); the key must match `recordings/<userId>/<projectId>/<recordingId>.<mp3|m4a|wav|webm|mp4>`, have a Recording item whose `storageKey` is exactly that key, and a Job owned by that `userId`. Anything else is logged and dropped.
- **`RetryRequested`** from the bus: rule `RetryRequested` → **the same queue** (target role `eventbridge-rules-role`). The SQS body is the EventBridge event. A direct EventBridge → Lambda target also works (`handler` checks `detail-type` first), but needs a Lambda resource policy and gives retries no DLQ, so the queue is the default.

## Upload path

1. Job must still be `Queued` at `retryCount` 0 (otherwise it's a duplicate or late event: done).
2. Download to `/tmp` → `probe` → `split` → upload `chunks/<jobId>/<n>.wav` (n from 0, deterministic keys).
3. **Prepare** write: `chunksTotal`, `audioSeconds`; remove `doneChunks`, `synthesisEnqueued`, `failureReason`.
4. One `TranscriptionChunk` message per chunk on `interlixir-transcription` (batches of 10; contract in `services/shared/contracts/messages.schema.json`).
5. `UploadReceived` on the bus (`eventId` = `uploadreceived-<jobId>`).
6. **Commit** write: stage `Transcribing`. The API now shows "Transcribing 0/N".

Both writes are conditional on *stage `Queued`, same `userId`, same `retryCount`*. Steps 2–5 are safe to repeat, so a failure anywhere before step 6 leaves the job `Queued`, the message comes back (3 receives, then `interlixir-upload-dlq`), and the run is redone. A repeated chunk message only costs worker time: transcript keys are deterministic and `doneChunks` is a set. The prepare write happens **before** the sends so it can never overwrite a worker's `ADD doneChunks`.

## Retry path (`RetryRequested`): resume from the earliest missing artifact

Only if the job is `Queued` at the event's `retryCount` (otherwise stale or already handled). Then, using S3 as the source of truth:

| What exists | What it does |
| --- | --- |
| `chunksTotal` set, every `transcripts/<userId>/<jobId>/chunks/<n>.json` present | prepare `doneChunks` = all, `synthesisEnqueued = true` → `SynthesisRequest` on `interlixir-synthesis` → commit |
| `chunksTotal` set, some transcripts missing, their chunk audio present | prepare `doneChunks` = the transcripts found, remove `synthesisEnqueued` → re-enqueue only the missing chunks → commit |
| no `chunksTotal` (it failed while `Queued`), or a missing transcript's chunk audio is gone | the upload path's steps 2–6 from the recording (no `UploadReceived`) |
| …and the recording object or item is gone | `Failed`: "The original recording is no longer stored…" |

Nothing in S3 expires, so the last row only happens if the project was deleted, and then the Job is normally gone too.

## Failures

- **The file's fault** (`media.InputError`: unreadable, no audio track, longer than `MAX_AUDIO_SECONDS` = 3 h): `JobFailed` event (`eventId` = `jobfailed-<jobId>-<retryCount>`, stage `Queued`), then stage `Failed` with a `failureReason` the user sees. The message is deleted; Retry would fail the same way.
- **Anything else** (S3/SQS/EventBridge errors, ffmpeg timeout, `/tmp` full) raises: the message is retried, then the DLQ. When the message reaches the DLQ, the dead-letter handler (`services/dlq-handler/`) marks the job `Failed` ("couldn't be prepared for transcription after several attempts").
- A last chunk under 1 s is dropped (AAC padding makes an exact 30:00 file decode a few samples long; it would be a 7th, empty chunk), unless it's the only chunk.

## Deployment

| Setting | Value |
| --- | --- |
| Image | ECR repo `interlixir-job-creator`; `make push-job-creator` builds the arm64 image (with ffmpeg) and pushes it tagged by git SHA |
| Function | `interlixir-job-creator`, container image, arm64, no VPC, no environment (code defaults: table, buckets, queue URLs, bus, `CHUNK_SECONDS = 300`, `MAX_AUDIO_SECONDS = 10800`) |
| Memory / ephemeral storage / timeout | 2048 MB / 1024 MB / 600 s. `/tmp` needs the input (≤ 200 MB) plus chunks (≈ 115 MB per hour of audio); a 30-minute file took about 16 s and 253 MB. The upload queue's visibility timeout is 3600 s (6 × the function timeout) |
| Trigger | SQS `interlixir-upload`, batch size 1, report batch item failures on |
| Rule | `interlixir-retry-requested` on `interlixir-bus`, pattern `{"source": ["interlixir.app"], "detail-type": ["RetryRequested"]}`, target SQS `interlixir-upload` |
