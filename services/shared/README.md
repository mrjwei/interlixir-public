# services/shared

Keep it thin: logging (structured JSON, EMF helpers: the log contract), ID/key builders (user-scoped), and the **event and message contracts** as JSON Schemas, which contract tests use.

## Event contract

| File | What |
| --- | --- |
| `contracts/events.schema.json` | Draft 2020-12 schema for an event as targets receive it: `source`, `detail-type`, and a closed `detail` per type |
| `contracts/events.py` | `SOURCE`, the detail-type lists, `occurred_at()`, `put_events_entry()`, `validate_event()` |
| `contracts/examples/*.json` | One valid event per detail-type (two for `DeleteRequested`). Test fixtures for consumers |
| `tests/test_event_contract.py` | Contract tests: `make test-contracts` (needs `jsonschema`) |

- **Bus** `interlixir-bus`, **source** `interlixir.app`.
- **Every detail** has `eventId`, `userId` (Cognito `sub`) and `occurredAt`.
  - `occurredAt` is UTC with milliseconds and fixed width (`2026-10-15T01:02:03.456Z`), so the history sort key `EVENT#<occurredAt>#<eventId>` orders by time.
  - IDs can't contain `#` or `/`, because they go into DynamoDB and S3 keys.
- **`eventId` is deterministic** wherever a producer can be retried:
  - the stream router uses the stream record's `eventID`
  - other producers derive it from the job and the step
  - EventBridge delivers at least once, and a producer that crashed re-publishes the same `eventId` with a later `occurredAt`. So the history writer dedupes on the `eventId` alone, through a claim item, not on the Event item's key.

| detail-type | Producer | Consumer | Detail beyond the common fields |
| --- | --- | --- | --- |
| `UploadReceived` | job creator | history writer | `jobId` |
| `TranscriptionDone` | Whisper worker (the chunk that completes the set) | history writer | `jobId` |
| `SynthesisDone` | synthesis | history writer | `jobId`, `tokenCount`, `cost` |
| `JobFailed` | job creator, synthesis, dead-letter handler | history writer | `jobId`, `stage` (where it failed), optional `tokenCount`, `cost` |
| `ChatSession` | chat agent | history writer | `chatSessionId`, `tokenCount`, `cost` |
| `StageChanged` | pipeline services | none yet | `jobId`, `stage` |
| `DeleteRequested` | stream router | GC | `entity` (`Project` / `Document`), `projectId`, `documentId` (exactly when `Document`); `jobId`, `recordingId` (optional, only when `Document`) |
| `RetryRequested` | stream router | job creator | `jobId`, `projectId`, `recordingId`, `retryCount` |

**History vs control.**
- The first five are **history events**: the data model limits an Event item's `eventType` to these. The history writer's rule matches only them.
- The last three are **control events**. They start work and aren't shown to the user.

**Where the stream router gets the fields:**
- **Project REMOVE:** `userId` from `PK` (`USER#…`), `projectId` from `SK`.
- **Document REMOVE:** `projectId` from `PK`, `documentId` from `SK`, and **`userId` from the item's `userId` attribute**, which synthesis writes (from the Job's `userId`). `jobId` and `recordingId` come from the item's attributes of those names (synthesis writes both; .
- **Job MODIFY:** `jobId` from `PK`; `userId`, `recordingId` and `retryCount` from the new image; `projectId` from `GSI1PK` (`PROJECT#…`).

**Producers:**
- Build entries with `put_events_entry()`.
- Check `FailedEntryCount` on the `PutEvents` response and raise if it's above 0. `PutEvents` doesn't throw on partial failure.
- `put_events_entry()` doesn't validate, so a Lambda zip needs nothing beyond boto3. Each producer's unit tests run their output through `validate_event()` instead.

**Changing the contract:**
- Edit the schema, the examples and `events.py` together, then run `make test-contracts`.
- Details are closed (`additionalProperties: false`). A new field means a schema change first, and then the producer.

## Queue message contract

| File | What |
| --- | --- |
| `contracts/messages.schema.json` | One closed `$defs` entry per SQS message body |
| `contracts/messages.py` | `MESSAGE_TYPES`, `validate_message(name, body)` (tests only, needs `jsonschema`) |
| `contracts/message_examples/*.json` | One valid body per type, fixtures for consumers |
| `tests/test_message_contract.py` | Contract tests, also run by `make test-contracts` |

| Message | Queue | Producer | Consumer | Body |
| --- | --- | --- | --- | --- |
| `TranscriptionChunk` | `interlixir-transcription` | job creator | Whisper worker | `jobId`, `userId`, `chunk` (0-based), `chunksTotal`, `startSeconds` |
| `SynthesisRequest` | `interlixir-synthesis` | the Whisper worker that completes the set; job creator on a retry that finds every transcript | synthesis | `jobId`, `userId` |

- Object keys aren't in the bodies: they follow from the IDs Audio `chunks/<jobId>/<chunk>.wav`; transcript `transcripts/<userId>/<jobId>/chunks/<chunk>.json`.
- `chunksTotal` travels with each chunk, so the "last chunk" check can compare against the message rather than depend on the job item's write order.
- **Delivery is at least once, and the job creator may send a chunk twice** (a redone run after a failure). Consumers must be idempotent: deterministic output keys, a set for `doneChunks`, and the conditional `synthesisEnqueued` before sending `SynthesisRequest`. Synthesis can also see a request twice (SQS itself, or concurrent retries), so its outputs need deterministic keys too.
- **`synthesisEnqueued` records who claimed the request:** the winning `TranscriptionChunk`'s SQS `MessageId` (Whisper worker), or `true` (the job creator's retry path). The worker's condition is `attribute_not_exists(synthesisEnqueued) OR synthesisEnqueued = <its own MessageId>`, so if the winner dies between the claim and the send, its own redelivery (same `MessageId`) can still send; no other message can.

## Chunk transcript contract

| File | What |
| --- | --- |
| `contracts/transcripts.schema.json` | The JSON at `transcripts/<userId>/<jobId>/chunks/<chunk>.json` |
| `contracts/transcripts.py` | `validate_transcript(body)` (tests only, needs `jsonschema`) |
| `contracts/transcript_example.json` | One valid transcript, a fixture for synthesis |
| `tests/test_transcript_contract.py` | Contract tests, also run by `make test-contracts` |

- Producer: Whisper worker. Consumer: synthesis, which merges chunks `0 … chunksTotal-1` in order.
- Fields: `jobId`, `chunk`, `startSeconds`, `model` (the checkpoint, e.g. `distil-small.en`), `language` (always `en`), `audioSeconds`, `text` (segments joined with spaces) and `segments[]` of `{start, end, text}`.
- **Segment times are seconds from the start of the recording**, not the chunk: the worker adds the message's `startSeconds`. Merged chunks keep the recording's timeline.
- A silent chunk is valid: `text` is empty and `segments` is `[]` (the VAD filter drops silence).
- No `userId` in the body: it's in the key, and synthesis already has it from the `SynthesisRequest`.

## Log contract

| File | What |
| --- | --- |
| `contracts/logs.py` | `configure(service)`, `bind()` / `scope()`, `info` / `warning` / `error`, `metric()` (an EMF line), `capture()` for tests |
| `tests/test_logs.py` | Line shape, binding, the EMF directive; run by `make test-contracts` |

Every service writes through it, so queries, dashboards and alarms can rely on the names. It's packaged like `events.py`: `scripts/build_lambda_zip.sh` copies it into each zip, and the job creator, worker and API images `COPY --from=shared` it.

- **One compact JSON object per line on stdout**, printed rather than passed to `logging`: the Lambda runtime prefixes `logging` output, and a prefixed line isn't EMF.
- **Fields on every line:** `service`, `level` (`INFO`, `WARNING`, `ERROR`), `msg`.
- **Fields on job lines:** `jobId`, `userId` (the Cognito `sub` only: never a name, an email, a file's content or model output about it), `stage` (the job stage the work belongs to: `Queued` for chunking, `Transcribing` for the worker and the merge, `Redacting`, `Synthesising`, `Done`, `Failed`), `durationMs` on the line that closes a unit of work. Lambdas add `requestId`, queue consumers `messageId`, the worker `chunk`.
- **`bind()` / `scope()`** attach fields to the following lines, for a process that handles one message at a time (the Lambdas, the worker). The binding is one dict, not a context variable, so the threads of the same message (redaction windows, the worker's heartbeat) share it. The API serves requests on a thread pool, so it passes its fields on each call instead.
- **Metrics are log lines with an `_aws` block** (namespace `interlixir`). A metric with a dimension is published per value and without it, so a total is a plain metric. `error()` adds `Errors` by `Service`. `metric(..., bound=False)` leaves out bound fields: the worker's 60 s meter isn't part of the chunk it runs beside.

| Metric | Unit | Dimension | Emitted by |
| --- | --- | --- | --- |
| `StepDuration` | Milliseconds | `Step` | job creator (`chunk`), worker (`transcribe`), synthesis (`merge`, `redact`, `synthesise`) |
| `JobDuration`, `JobsDone` | Milliseconds, Count | | synthesis on `Done` (`JobDuration` from the job's `createdAt`, first runs only) |
| `JobsFailed` | Count | | job creator, synthesis, dead-letter handler |
| `Errors` | Count | `Service` | every service, on an unexpected exception; the API and the chat agent on a 5xx |
| `LlmTokens`, `LlmCost` | Count, None (USD) | `Model` | synthesis and the chat agent, per Bedrock call |
| `WorkerTaskSeconds`, `FargateCost` | Seconds, None (USD) | | worker, every 60 s |
| `RequestLatency` | Milliseconds | `Route` (`GET /a3/jobs/{job_id}`, `POST /acp/messages`) | API, chat agent (for the stream: to the first byte) |
| `ChatTurnDuration` | Milliseconds | | chat agent, per answered message |
| `ToolLatency` | Milliseconds | `Tool` | MCP server, per tool call |
| `CacheHit`, `CacheMiss` | Count | `Route` | API |

**Changing it:** a renamed field or metric breaks any query, dashboard or alarm built on it, so change them together.
