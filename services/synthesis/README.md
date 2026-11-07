# services/synthesis

Synthesis. **Zip** Lambda (Python 3.14, arm64), **outside the VPC**, on the synthesis queue. It turns a job whose chunks are all transcribed into a redacted, schema-validated Markdown document, stores it, and sets the job `Done`.

| File | What |
| --- | --- |
| `handler.py` | Entry point, the four steps, job state (conditional stage moves, `fail`), the commit transaction, S3/SQS/EventBridge |
| `bedrock.py` | Converse calls: `structured()` (forced tool for Nemotron, JSON in text for Gemma, one regeneration with the errors), `text()`, `Usage` (tokens per model, cost), `Clock` (time left in the invocation) |
| `redaction.py` | PII: the model lists entities per window, code replaces them with consistent pseudonyms; regex backstop for emails and phone/ID numbers |
| `synthesise.py` | The prompt (template, instruction, context files, redacted transcript in tags), the `write_document` tool, the validator, the title |
| `render.py` | Markdown from the validated result, driven by the template's schema (works for any template, incl. M19's custom ones) |
| `extract.py` | Context file text: PDF (pypdf), DOCX (zip + XML, standard library), MD, TXT |
| `requirements.txt` | `jsonschema`, `pypdf`: bundled into the zip as arm64 Linux wheels by `make zip-synthesis` (boto3 comes with the runtime) |
| `tests/` | `make test-synthesis` (47 tests; moto + a fake Bedrock). One-time setup: `make venv-synthesis` |

## Trigger

SQS `interlixir-synthesis`, one `SynthesisRequest` (`jobId`, `userId`; `services/shared/contracts/messages.schema.json`) per job. Producers: the G8 winner (Whisper worker) and the job creator when a retry finds every transcript. The request can arrive twice, so everything here is idempotent.

What it accepts: the Job exists with that `userId`, is `Transcribing`, `Redacting` or `Synthesising`, and has `synthesisEnqueued`. A `Done` job only gets `SynthesisDone` again (same `eventId`; in case the run that finished it died before sending). Anything else (deleted, `Queued`, `Failed`, claim reset by a retry) is logged and dropped.

## Steps

Each step saves its output under `transcripts/<userId>/<jobId>/` before the next starts. A redelivery or a later invocation **resumes after the last saved step** instead of paying for the Bedrock calls again. A saved step is reused only if its `chunksTotal` matches the job's (a re-chunked job starts over).

| # | Stage | What | Saved as |
| --- | --- | --- | --- |
| 1 | (stays `Transcribing`) | Merge `chunks/0..N-1.json` in chunk order (segment times are already from the start of the recording). Sets the Recording's `transcriptKey`. No speech at all → `Failed` | `merged.json` |
| 2 | `Redacting` | PII → pseudonyms (below) | `redacted.json` (segments, entity counts per type, model, tokens; **not** the mapping back to the real values) |
| 3 | `Synthesising` | Template + instruction + context text + redacted transcript → forced `write_document` call → validate → regenerate once if invalid → title | `document.json` (result, title, template schema, context file statuses, tokens) |
| 4 | `Done` | Render Markdown → `documents/<userId>/<projectId>/<jobId>.md` in `interlixir-docs`; **one transaction**: project still exists + Document item + Job `Done`; then `SynthesisDone` | the document |

- **documentId = jobId.** One job makes one document, so a repeated run overwrites instead of adding a second.
- **The commit is one `TransactWriteItems` of three `Update`s** (so it needs only `dynamodb:UpdateItem`; `ConditionCheckItem` is denied for the Lambda role, the account's permission limits): the Project item (`SET lastDocumentAt`, condition `attribute_exists(PK)`: the "project still exists" check, which can't recreate a deleted project), the Document item, created by the update (`documentTitle`, `storageKey`, `jobId`, **`userId`** for the stream router, `recordingId`, `schemaId`, `contextFiles`, `createdAt`), and the Job (`stage = Done`, `tokenCount`, `cost`, `documentId`; condition: still this user's and `Synthesising`). The Document appears exactly when the job is `Done`. If the project was deleted meanwhile, the S3 object is removed again and nothing is written; GC has already swept the project, so a late Document would be an orphan.
- **Stages only move forward**, by conditional writes. A run that finds the job already past a stage doesn't move it back; a run whose write is refused (deleted, failed, or finished by a duplicate) stops.
- **Tokens and cost:** every Bedrock call's usage is added up per model and saved with its step, so a resumed run still counts what the earlier run spent. Cost = tokens × the price table (USD per million tokens, per model), read from Parameter Store `/interlixir/model-prices` (see Price table below). On the Job item and in `SynthesisDone`; `JobFailed` carries what was spent before a failure. Each call is also logged as it happens, as the `LlmTokens` / `LlmCost` metrics by model (`docs/observability/`), so calls whose result was lost still count on the dashboard.

## Redaction

The model **lists** the PII; code **replaces** it. It doesn't rewrite the transcript:
- The reply is a short list rather than a second copy of a transcript of up to ~45K tokens. That keeps redaction within one invocation and costs a fraction of the output tokens.
- Pseudonyms are assigned in code, so they're consistent across windows and runs: `[Person 1]`, `[Person 2]` by first appearance, `[Organisation A]`, `[Email 1]`, `[Phone 1]`, `[Address 1]`, `[ID 1]`. Each entity comes with `refersTo` (the fullest form of the name), and a one-word name joins the one full name it starts or ends, so "Sarah" and "Sarah Chen" share `[Person 1]`.
- An entity that isn't in the transcript word for word is dropped and counted (`entities.unmatched`), so a made-up entity can't change the text.
- **Short forms:** the first or last word of a reported full name is also replaced wherever it stands alone, capitalised (`entities.shortForms`). Both models list "Sarah Mitchell" but often skip the "Sarah" a few lines later; titles ("Mrs") and words under 3 letters aren't taken, and an ambiguous one ("Sarah" with two Sarahs) gets its own pseudonym.
- **Lower case:** a name reported capitalised doesn't replace the same word in lower case, so "Rose" leaves the verb "rose" (and a participant called Will doesn't take every "will").
- Windows of ≤ **2000** characters (was 8000 until M14: smaller windows lost fewer names), 3 at a time (`REDACTION_WINDOW_CHARS`, `REDACTION_PARALLELISM`), forced tool `report_pii` on Nemotron (`REDACTION_MODEL_ID`; Gemma works too, through the JSON-in-text path).
- Regex backstop: email addresses, Australian phone numbers (`0` or `+61` then 9 digits, or `(0x)` then 8; **commas allowed**, because Whisper writes spoken digit groups as "0412, 345, 678"), and other 8+ digit runs as ID numbers.
- **Evaluation:** `make eval-pii` scores this code on a labelled set (`eval/`, the build logs): Nemotron + regex removes 53 of 54 labelled entities with precision 0.98 one sample per call, and 51–52 in production-sized windows. Gemma is as good on short excerpts but drops to 47 of 54 in a long window; Presidio (regex + spaCy NER) reaches 40 of 54 and redacts brands and cities.
- Limits (report material): an entity split across two segments isn't replaced; context files aren't redacted (they're the researcher's own material); speakers aren't labelled (Whisper doesn't diarise), so the synthesis model infers "Interviewer" / "Participant 1" from the conversation.

## Synthesis

- **Forced tool use on Nemotron:** the template's JSON Schema (minus `$schema`) is the input schema of `write_document`, and `toolChoice` forces it. The tool call's input is the document.
- **Repaired, then validated:** `synthesise.repair` first fixes what code can fix safely: a list cut to its `maxItems`, a list or object sent as a JSON string parsed, a list of strings sent as lines split. In the M14 template runs that took first-try validity from 11/18 to 16/18 (with the template changes) and halved the time on the largest template (`make try-templates`).
- **Validated** with `jsonschema` (`Draft202012Validator`, the templates' dialect). If invalid, the errors go back as the tool result and the model gets one more try. Still invalid → `Failed` ("didn't match the template twice in a row"), because retrying the same input is unlikely to help. A reply cut off at `SYNTHESIS_MAX_TOKENS` (8192) counts as invalid.
- **Prompt:** the template's name and description, the researcher's instruction, the context files (in upload order, up to `CONTEXT_CHAR_BUDGET` = 40,000 characters in total: the file that crosses the budget is cut, later ones are left out) and the redacted transcript as `[mm:ss] text` lines, each in its own tag. The system prompt says the tags are data and can't change the rules (prompt injection from a transcript or an instruction).
- **Context files:** text is extracted once and saved beside the file as `<key>.txt` (reused by retries and the other jobs of the same upload). Each file's status goes on the Document item and into the document's "About" section: `full`, `truncated`, `omitted`, `unreadable` (corrupt or encrypted), `missing` (presigned but never uploaded).
- **Title:** Gemma (`TITLE_MODEL_ID`), plain text, ≤ 8 words. If the call fails, the reply is unusable or under a minute is left, the title is "`<template>: <file name>`". A title is never worth failing a document over.
- **Markdown** is rendered from the result by walking the schema (section order = property order; lists of quotes become block quotes with speaker and timestamp; other object lists become sub-sections). It ends with "About this document": recording, template, instruction, context files, generated time, models.

## Failures

| What | Result |
| --- | --- |
| No speech in any chunk | `JobFailed` (stage `Transcribing`), then `Failed`: "No speech was found…" |
| The template item is gone | `Failed`: "The template this job used no longer exists…" |
| Output still invalid after the regeneration | `JobFailed` (stage `Synthesising`, with tokens and cost), then `Failed` |
| Anything else (Bedrock throttling after retries, S3/DynamoDB errors, a missing chunk transcript) | Raises: the message comes back after **60 s × its receive count** (30 s until M17: the 3 receives fitted inside a 2.5 min spell of invalid Nemotron replies, the build logs) (`ChangeMessageVisibility` here; otherwise it would wait out the queue's 3600 s), and after 3 receives it goes to `interlixir-synthesis-dlq`. The dead-letter handler (`services/dlq-handler/`) then marks the job `Failed`: "The document couldn't be written after several attempts…"; Retry resumes from the saved steps |
| Not enough time left for another Bedrock call (`READ_TIMEOUT` 300 s + 20 s) | Released at once (visibility 0); the next invocation resumes from the saved steps. Each release still counts as a receive |

Bedrock calls use adaptive retries (3 attempts, back off on throttling; the quotas are shared by the whole account) and a 300 s read timeout: Converse isn't streamed, so that bounds one call.

## Console settings

| Setting | Value |
| --- | --- |
| Code | `make zip-synthesis` → `dist/synthesis.zip` (≈ 1 MB). Upload in the console (Code → Upload from → .zip file) |
| Function | `interlixir-synthesis`, **Python 3.14**, **arm64**, handler `handler.handler` (set it **before** adding the trigger: see below), role `lambda-role`, no VPC, both tags |
| Memory / timeout | **1024 MB / 600 s** (10 min). Mostly waiting on Bedrock, but pypdf on a large PDF needs headroom; check "Max Memory Used" in the REPORT lines (`make check-synthesis` prints them) |
| Environment | Prices come from Parameter Store (Price table, below). `MODEL_PRICES` (same JSON) is only a fallback while the parameter can't be read. Without either, cost is recorded as 0 and each run logs `no price for model`. Everything else has a code default (table, buckets, queue URL, bus, model IDs, budgets) |
| Trigger | SQS `interlixir-synthesis`, batch size 1, batching window 0, **Report batch item failures** on, **maximum concurrency 2** (the lowest allowed; Bedrock quotas are shared), both tags |
| Synthesis queue | **Visibility timeout 1800 → 3600 s** (6 × the 600 s timeout, AWS's guidance for a Lambda-polled queue). It only matters when an invocation dies without releasing its message (timeout, crash); normal failures set their own backoff |

### Price table

Systems Manager → Parameter Store → Create parameter: name **`/interlixir/model-prices`**, tier Standard, type **String**, data type text, both tags, value (USD per million tokens, Bedrock pricing page, Sydney, on-demand):

`{"nvidia.nemotron-super-3-120b": {"input": 0.15, "output": 0.67}, "google.gemma-3-12b-it": {"input": 0.0927, "output": 0.2987}}`

`lambda-role` and the chat agent's task role need `ssm:GetParameter` on it. Each process caches the table for 60 s and gives up on a read after 3 s, keeping the last good table. Redeploy synthesis (`make zip-synthesis`) and the chat agent (`make push-chat-agent`, new task definition revision, drop `MODEL_PRICES`). Check: edit the value (e.g. double Nemotron's input price), run a job, and the next job's `cost` doubles its input part with no redeploy. `aws ssm get-parameter --name /interlixir/model-prices` reads it back. A read failure logs `model price table not read from Parameter Store`.

Read-only checks after creating it: `aws lambda get-function-configuration --function-name interlixir-synthesis`, `aws lambda list-event-source-mappings --function-name interlixir-synthesis`, `aws sqs get-queue-attributes --queue-url <synthesis> --attribute-names VisibilityTimeout`.

The first run (2026-10-16) settled most of the account's permission limits: Bedrock, both buckets, `ChangeMessageVisibility` and `PutEvents` work; `ConditionCheckItem` is denied, hence the `Update`-only commit. Any `AccessDenied` shows in the function's log with the failing call.

**Pitfalls**
- **Set the handler before adding the SQS trigger.** A new console function's handler is `lambda_function.lambda_handler`. On the first deploy the trigger delivered the waiting requests before it was changed: 3 `Runtime.ImportModuleError` invocations, each counting as a receive, so the real job reached the DLQ after only 2 real attempts (`maxReceiveCount` 3). Upload the zip and set `handler.handler` first, or add the trigger disabled.
- Tag keys are free text: check them after saving (`aws lambda get-function --query Tags`). The first save stored `" purpose"` with a leading space.

Milestone: **M8**.
