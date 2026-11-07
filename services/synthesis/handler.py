"""Synthesis: a job whose chunks are all transcribed -> a Markdown document.

One SynthesisRequest (services/shared/contracts/messages.schema.json) per job arrives on interlixir-synthesis
(event source mapping: batch size 1, ReportBatchItemFailures, maximum concurrency 2 for the shared Bedrock
quotas). Each step saves its output before the next starts, so a redelivery or a later invocation carries on
after the last saved step instead of paying for the Bedrock calls again:

  merge the chunk transcripts in order  -> transcripts/<userId>/<jobId>/merged.json    (stage stays Transcribing)
  Redacting:    PII -> pseudonyms       -> transcripts/<userId>/<jobId>/redacted.json
  Synthesising: template + instruction + context text + redacted transcript -> forced tool call -> validate
                (regenerate once if invalid) -> title -> transcripts/<userId>/<jobId>/document.json
  render Markdown -> documents/<userId>/<projectId>/<documentId>.md in the docs bucket
  one transaction: the project still exists + the Document item + the Job Done with tokenCount and cost
  SynthesisDone (eventId synthesisdone-<jobId>-<retryCount>)

Idempotent, because the request can arrive twice: the documentId is the jobId (one job, one
document), every key is deterministic, the stage only moves forward through conditional writes, and a request
for a job that is already Done only re-sends SynthesisDone (same eventId; the history writer dedupes), in
case the run that finished it died before sending.

Failures:
- the input can't work (no speech, the template is gone, the output is still invalid after the regeneration):
  JobFailed, then stage Failed with a reason the user sees. The message is deleted.
- anything else raises: the message comes back after 60 s x its receive count (set here; otherwise it would
  wait out the queue's visibility timeout), and goes to the DLQ after 3 receives, where the dead-letter handler marks the job Failed
- too little time left for another Bedrock call: the message is released at once and the next invocation
  resumes from the saved steps

Lambdas can't reach the cache, so the API's job status and lists catch up on their own TTLs.
"""

import datetime
import decimal
import json
import os
import re
import time
import traceback

import bedrock
import extract
import redaction
import render
import synthesise
from contracts import events, logs

logs.configure("synthesis")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "interlixir-uploads")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "interlixir-docs")
_QUEUES = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/"
SYNTHESIS_QUEUE_URL = os.environ.get("SYNTHESIS_QUEUE_URL", _QUEUES + "interlixir-synthesis")
BUS_NAME = os.environ.get("BUS_NAME", "interlixir-bus")
# G5: Nemotron for anything structured (native tool use); Gemma for the title. Redaction stays on Nemotron
# until M14 shows whether Gemma's recall is close enough.
SYNTHESIS_MODEL_ID = os.environ.get("SYNTHESIS_MODEL_ID", bedrock.NEMOTRON)
REDACTION_MODEL_ID = os.environ.get("REDACTION_MODEL_ID", bedrock.NEMOTRON)
TITLE_MODEL_ID = os.environ.get("TITLE_MODEL_ID", bedrock.GEMMA)
SYNTHESIS_MAX_TOKENS = int(os.environ.get("SYNTHESIS_MAX_TOKENS", "8192"))
# About 10K tokens of context files, on top of a transcript of up to ~45K tokens at the 3 h limit.
CONTEXT_CHAR_BUDGET = int(os.environ.get("CONTEXT_CHAR_BUDGET", "40000"))
# 2000, not 8000: smaller windows lost fewer names in the M14 evaluation (recall 0.96 -> 0.98,
# the build logs).
REDACTION_WINDOW_CHARS = int(os.environ.get("REDACTION_WINDOW_CHARS", "2000"))
REDACTION_PARALLELISM = int(os.environ.get("REDACTION_PARALLELISM", "3"))
# A failed message is visible again after this many seconds x its receive count. 60, not 30: in M17's steady run
# Nemotron gave invalid redaction replies for about 2.5 min, and at 30 s all 3 receives fell inside that spell
# (the build logs).
RETRY_BACKOFF_SECONDS = 60

ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
JOB_SK = "META"
WORKING = ("Transcribing", "Redacting", "Synthesising")

_clients = {}


class Stale(Exception):
    """The job moved on while this run was working (deleted, Failed, or finished by a duplicate request)."""


def _client(name):
    if name not in _clients:
        import boto3  # in the Lambda runtime; imported here so the pure modules' tests don't need it
        from botocore.config import Config

        if name == "bedrock-runtime":
            # Adaptive retries back off on throttling (the Bedrock quotas are shared by the whole account).
            config = Config(connect_timeout=5, read_timeout=bedrock.READ_TIMEOUT,
                            retries={"mode": "adaptive", "max_attempts": 3})
        else:
            config = Config(connect_timeout=5, read_timeout=60, retries={"mode": "standard", "max_attempts": 5})
        if name == "table":
            _clients[name] = boto3.resource("dynamodb", region_name=REGION, config=config).Table(TABLE_NAME)
        else:
            _clients[name] = boto3.client(name, region_name=REGION, config=config)
    return _clients[name]


def _log(msg, **fields):
    logs.info(msg, **fields)


def _conditional_failed():
    return _client("table").meta.client.exceptions.ConditionalCheckFailedException


# --- keys (layout in the deployment notes) --------------------------------------------------------------


def chunk_transcript_key(user_id, job_id, n):
    return f"transcripts/{user_id}/{job_id}/chunks/{n}.json"


def step_key(user_id, job_id, step):
    """merged, redacted or document: the saved output of each step, beside the chunk transcripts."""
    return f"transcripts/{user_id}/{job_id}/{step}.json"


def document_key(user_id, project_id, document_id):
    """The same key the API builds (services/api/app/keys.py document_object_key)."""
    return f"documents/{user_id}/{project_id}/{document_id}.md"


# --- entry point -------------------------------------------------------------------------------------


def handler(event, context):
    clock = bedrock.Clock(context)
    failures = []
    for record in event["Records"]:
        with logs.scope(requestId=getattr(context, "aws_request_id", None), messageId=record["messageId"]):
            try:
                process(parse(record["body"]), clock)
            except bedrock.OutOfTime as error:
                _log("out of time, released to carry on in a new invocation", reason=str(error))
                failures.append({"itemIdentifier": record["messageId"]})
                _release(record, 0)
            except Exception as error:
                logs.error("message failed, will be redelivered", error=repr(error), trace=traceback.format_exc())
                failures.append({"itemIdentifier": record["messageId"]})
                receives = int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
                _release(record, RETRY_BACKOFF_SECONDS * receives)
    return {"batchItemFailures": failures}


def parse(raw):
    """A SynthesisRequest body, checked enough to build keys from it. A bad body raises, so it ends in the DLQ
    where it can be read, rather than being dropped."""
    body = json.loads(raw)
    for name in ("jobId", "userId"):
        if not isinstance(body.get(name), str) or not ID.fullmatch(body[name]):
            raise ValueError(f"bad {name} in message")
    return body


def process(body, clock):
    job_id, user_id = body["jobId"], body["userId"]
    logs.bind(jobId=job_id, userId=user_id)
    job = _get(f"JOB#{job_id}", JOB_SK)
    if job is not None:
        logs.bind(stage=job.get("stage"), retryCount=int(job.get("retryCount", 0)))
    if job is None or job.get("userId") != user_id:
        _log("skipped: job is gone", jobId=job_id)  # deleted with its project
        return
    stage = job.get("stage")
    if stage == "Done":
        _synthesis_done(job)
        _log("already done, SynthesisDone sent again", jobId=job_id)
        return
    if stage not in WORKING or "synthesisEnqueued" not in job:
        # Queued or Failed (a retry is under way or finished differently), or the claim was reset by a retry.
        _log("skipped: stale request", jobId=job_id, stage=stage)
        return
    try:
        run(job, clock)
    except Stale as error:
        _log("stopped: job moved on", jobId=job_id, reason=str(error))


def run(job, clock):
    job_id, user_id = _job_id(job), job["userId"]
    project_id = job["GSI1PK"].removeprefix("PROJECT#")
    usage = bedrock.Usage()

    merged = merged_transcript(job, project_id)
    if not merged["segments"]:
        fail(job, "No speech was found in the recording, so there was nothing to write a document from.", usage)
        return

    _advance(job, "Redacting")
    redacted = redacted_transcript(job, merged, clock)
    usage.merge(bedrock.Usage(redacted["usage"]))

    _advance(job, "Synthesising")
    result = synthesis_result(job, project_id, redacted, usage, clock)
    if result is None:
        return
    usage.merge(bedrock.Usage(result["usage"]))
    store_document(job, project_id, result, usage)


# --- step 1: merge -----------------------------------------------------------------------------------


def merged_transcript(job, project_id):
    """Every chunk's segments in chunk order (their times are already from the start of the recording)."""
    job_id, user_id, total = _job_id(job), job["userId"], int(job["chunksTotal"])
    key = step_key(user_id, job_id, "merged")
    merged = _saved(key, total)
    if merged is None:
        started = time.monotonic()
        segments, audio_seconds, models = [], 0.0, set()
        for n in range(total):
            chunk = _read_json(UPLOADS_BUCKET, chunk_transcript_key(user_id, job_id, n))
            if chunk is None:  # synthesisEnqueued is only claimed once every chunk is recorded
                raise RuntimeError(f"transcript of chunk {n} is missing")
            segments.extend({"start": s["start"], "end": s["end"], "text": s["text"]} for s in chunk["segments"])
            audio_seconds += float(chunk["audioSeconds"])
            models.add(chunk["model"])
        merged = {
            "jobId": job_id, "chunksTotal": total, "audioSeconds": round(audio_seconds, 1),
            "models": sorted(models), "segments": segments,
        }
        _put_json(UPLOADS_BUCKET, key, merged)
        _step("merged", "merge", started, chunks=total, segments=len(segments), audioSeconds=merged["audioSeconds"])
    # Drawio: the Recording points at its transcript. Conditional, so a deleted Recording isn't recreated.
    try:
        _client("table").update_item(
            Key={"PK": f"PROJECT#{project_id}", "SK": f"REC#{job['recordingId']}"},
            UpdateExpression="SET transcriptKey = :key",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":key": key},
        )
    except _conditional_failed():
        pass
    return merged


# --- step 2: redact -----------------------------------------------------------------------------


def redacted_transcript(job, merged, clock):
    job_id, total = _job_id(job), merged["chunksTotal"]
    key = step_key(job["userId"], job_id, "redacted")
    redacted = _saved(key, total)
    if redacted is not None:
        return redacted
    started = time.monotonic()
    usage = bedrock.Usage()
    entities = redaction.find_entities(
        _client("bedrock-runtime"), REDACTION_MODEL_ID, transcript_lines(merged["segments"]), usage, clock,
        REDACTION_WINDOW_CHARS, REDACTION_PARALLELISM,
    )
    segments, counts = redaction.pseudonymise(merged["segments"], entities)
    redacted = {
        "jobId": job_id, "chunksTotal": total, "audioSeconds": merged["audioSeconds"], "model": REDACTION_MODEL_ID,
        "entities": counts, "segments": segments, "usage": usage.by_model,
    }
    _put_json(UPLOADS_BUCKET, key, redacted)
    _step("redacted", "redact", started, modelId=REDACTION_MODEL_ID, entities=counts, tokens=usage.tokens())
    return redacted


# --- step 3: synthesise -------------------------------------------------------------------------


def synthesis_result(job, project_id, redacted, usage_so_far, clock):
    """The validated document plus what's needed to render and store it, or None if the job was failed."""
    job_id, user_id, total = _job_id(job), job["userId"], redacted["chunksTotal"]
    key = step_key(user_id, job_id, "document")
    result = _saved(key, total)
    if result is not None:
        return result

    started = time.monotonic()
    schema_item = _schema(user_id, job["schemaId"])
    if schema_item is None:
        fail(job, "The template this job used no longer exists, so no document was written. "
                  "Upload the recording again and pick another template.", usage_so_far)
        return None
    schema = json.loads(schema_item["definition"])
    template = schema_item.get("name") or schema.get("title") or job["schemaId"]
    contexts, context_files = context_texts(job, project_id)
    recording = _get(f"PROJECT#{project_id}", f"REC#{job['recordingId']}") or {}

    usage = bedrock.Usage()
    user_prompt = synthesise.prompt(
        template, schema.get("description"), job.get("instructionText"), contexts,
        transcript_lines(redacted["segments"]),
    )
    client = _client("bedrock-runtime")
    try:
        document = synthesise.synthesise(
            client, SYNTHESIS_MODEL_ID, schema, template, user_prompt, usage, clock, SYNTHESIS_MAX_TOKENS
        )
    except bedrock.InvalidOutput as error:
        _log("synthesis output invalid twice", jobId=job_id, errors=error.errors[:20])
        fail(job, "The model's output didn't match the template twice in a row, so no document was written. "
                  "Retry the job, or upload the recording again with another template.",
             bedrock.Usage(usage_so_far.by_model).merge(usage))
        return None
    title = synthesise.title(client, TITLE_MODEL_ID, template, document, usage, clock)
    result = {
        "jobId": job_id,
        "chunksTotal": total,
        "schemaId": job["schemaId"],
        "templateName": template,
        "schema": schema,
        "title": title or f"{template}: {recording.get('fileName') or 'recording'}",
        "titleFallback": title is None,
        "document": document,
        "recordingFileName": recording.get("fileName"),
        "contextFiles": context_files,
        "models": {"redaction": redacted.get("model"), "synthesis": SYNTHESIS_MODEL_ID, "title": TITLE_MODEL_ID},
        "usage": usage.by_model,
    }
    _put_json(UPLOADS_BUCKET, key, result)
    _step("synthesised", "synthesise", started, modelId=SYNTHESIS_MODEL_ID, template=job["schemaId"],
          contextFiles=context_files, tokens=usage.tokens(), titleFallback=title is None)
    return result


def context_texts(job, project_id):
    """([(fileName, text)] within CONTEXT_CHAR_BUDGET, [{"fileName", "status"}]) for the job's context files,
    in upload order. status: full, truncated (cut at the budget), omitted (no budget left), unreadable, or
    missing (never uploaded). Extracted text is saved beside the file as <key>.txt, for retries and for the
    other jobs of the same upload."""
    user_id = job["userId"]
    links = _query(f"JOB#{_job_id(job)}", "CTX#")
    budget, contexts, files = CONTEXT_CHAR_BUDGET, [], []
    for link in links:
        item = _get(f"PROJECT#{project_id}", link["SK"])
        if item is None:
            continue
        name, key = item.get("fileName") or "context file", item.get("storageKey", "")
        status, text = "missing", None
        # Built by the API from the verified userId; anything else isn't this user's file.
        if key.startswith(f"context/{user_id}/{project_id}/"):
            status, text = _context_text(key)
        if text is not None:
            if budget <= 0:
                status = "omitted"
            elif len(text) > budget:
                status, text = "truncated", text[:budget]
            else:
                status = "full"
            if status != "omitted":
                contexts.append((name, text))
                budget -= len(text)
        files.append({"fileName": name, "status": status})
    return contexts, files


def _context_text(key):
    cached = _read_bytes(UPLOADS_BUCKET, key + ".txt")
    if cached is not None:
        return "read", cached.decode("utf-8")
    data = _read_bytes(UPLOADS_BUCKET, key)
    if data is None:
        return "missing", None
    try:
        text = extract.extract(key.rsplit(".", 1)[-1].lower(), data)
    except extract.Unreadable as error:
        _log("context file unreadable, left out", key=key, reason=str(error))
        return "unreadable", None
    _client("s3").put_object(Bucket=UPLOADS_BUCKET, Key=key + ".txt", Body=text.encode("utf-8"),
                             ContentType="text/plain; charset=utf-8")
    return "read", text


# --- step 4: store -----------------------------------------------------------------------------------


def store_document(job, project_id, result, usage):
    job_id, user_id = _job_id(job), job["userId"]
    document_id = job_id  # one job, one document: a repeated run overwrites instead of adding a second
    key = document_key(user_id, project_id, document_id)
    tokens, cost = usage.tokens(), usage.cost(bedrock.prices())
    now = events.occurred_at()

    markdown = render.render(result["title"], result["schema"], result["document"], about(job, result, now))
    _client("s3").put_object(Bucket=DOCS_BUCKET, Key=key, Body=markdown.encode("utf-8"),
                             ContentType="text/markdown; charset=utf-8")

    document = {
        "documentTitle": result["title"],
        "storageKey": key,
        "jobId": job_id,
        "userId": user_id,  # the stream router names the owner of a removed Document from this
        "recordingId": job["recordingId"],
        "schemaId": result["schemaId"],
        "contextFiles": result["contextFiles"],
        "createdAt": now,
    }

    # All or nothing: the Document appears exactly when the Job is Done, and only if the project still exists
    # (a Document written after GC swept a deleted project would never be cleaned up).
    # Every part is an Update, so the commit needs only dynamodb:UpdateItem: lambda-role is denied
    # ConditionCheckItem. The project check is therefore a conditional Update, and the
    # Document is created by an Update (an update of a missing key creates the item, like the Put it replaces).
    client = _client("table").meta.client
    try:
        client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": {"PK": f"USER#{user_id}", "SK": f"PROJECT#{project_id}"},
                "UpdateExpression": "SET lastDocumentAt = :now",
                # Without the condition the update would recreate a deleted project as a nameless item.
                "ConditionExpression": "attribute_exists(PK)",
                "ExpressionAttributeValues": {":now": now},
            }},
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": {"PK": f"PROJECT#{project_id}", "SK": f"DOC#{document_id}"},
                "UpdateExpression": "SET " + ", ".join(f"#{name} = :{name}" for name in document),
                "ExpressionAttributeNames": {f"#{name}": name for name in document},
                "ExpressionAttributeValues": {f":{name}": value for name, value in document.items()},
            }},
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": {"PK": job["PK"], "SK": JOB_SK},
                "UpdateExpression": "SET #stage = :done, tokenCount = :tokens, cost = :cost, documentId = :doc "
                                    "REMOVE failureReason",
                "ConditionExpression": "userId = :userId AND #stage = :synthesising",
                "ExpressionAttributeNames": {"#stage": "stage"},
                "ExpressionAttributeValues": {
                    ":done": "Done", ":tokens": tokens, ":cost": decimal.Decimal(str(cost)), ":doc": document_id,
                    ":userId": user_id, ":synthesising": "Synthesising",
                },
            }},
        ])
    except client.exceptions.TransactionCanceledException as error:
        reasons = [r.get("Code") for r in error.response.get("CancellationReasons", [])]
        if reasons[:1] == ["ConditionalCheckFailed"]:
            _client("s3").delete_object(Bucket=DOCS_BUCKET, Key=key)
            _log("skipped: project was deleted, document removed", jobId=job_id, projectId=project_id)
            return
        raise Stale(f"commit refused: {reasons}") from None

    job.update(stage="Done", tokenCount=tokens, cost=cost)
    _synthesis_done(job)
    # The document title is model output about the recording, so it isn't logged (no PII in logs).
    # JobDuration is upload to document, for first runs only: a retry's createdAt is the original upload's.
    values = {"JobsDone": (1, "Count")}
    duration = None if int(job.get("retryCount", 0)) else _since(job.get("createdAt"))
    if duration is not None:
        values["JobDuration"] = (duration, "Milliseconds")
    logs.metric("INFO", "done", values, stage="Done", documentId=document_id, tokens=tokens, cost=cost,
                usage=usage.by_model, markdownBytes=len(markdown.encode("utf-8")), durationMs=duration)


def about(job, result, generated_at):
    files = ", ".join(
        f["fileName"] + ("" if f["status"] == "full" else f" ({f['status']})") for f in result["contextFiles"]
    )
    models = result["models"]
    return [
        ("Recording", result.get("recordingFileName")),
        ("Template", result["templateName"]),
        ("Instruction", job.get("instructionText")),
        ("Context files", files),
        ("Personal information", "removed before synthesis; names and contact details are replaced by labels such as [Person 1]"),
        ("Generated", generated_at),
        ("Models", f"redaction {models['redaction']}, synthesis {models['synthesis']}, title {models['title']}"),
    ]


# --- job state ---------------------------------------------------------------------------------------


def _advance(job, stage):
    """Move the job forward to stage, never back: a resumed run that's already past it does nothing."""
    order = list(WORKING)
    if order.index(job["stage"]) >= order.index(stage):
        return
    before = order[: order.index(stage)]
    names = {f":s{i}": s for i, s in enumerate(before)}
    try:
        _client("table").update_item(
            Key={"PK": job["PK"], "SK": JOB_SK},
            UpdateExpression="SET #stage = :stage",
            ConditionExpression=f"userId = :userId AND #stage IN ({', '.join(names)})",
            ExpressionAttributeNames={"#stage": "stage"},
            ExpressionAttributeValues={":stage": stage, ":userId": job["userId"], **names},
        )
    except _conditional_failed():
        raise Stale(f"could not move to {stage}") from None
    job["stage"] = stage
    logs.bind(stage=stage)


def fail(job, reason, usage):
    """The input can't produce a document: Failed with a reason the user sees, plus JobFailed for their
    history (with what it spent). The event goes first, so a crash between the two re-emits it (same eventId)
    rather than losing it."""
    job_id, retry_count = _job_id(job), int(job.get("retryCount", 0))
    detail = {"eventId": f"jobfailed-{job_id}-{retry_count}", "userId": job["userId"], "jobId": job_id,
              "stage": job["stage"]}
    if usage.tokens():
        detail.update(tokenCount=usage.tokens(), cost=usage.cost(bedrock.prices()))
    _put_event("JobFailed", detail)
    names = {f":s{i}": s for i, s in enumerate(WORKING)}
    try:
        _client("table").update_item(
            Key={"PK": job["PK"], "SK": JOB_SK},
            UpdateExpression="SET #stage = :failed, failureReason = :reason",
            ConditionExpression=f"userId = :userId AND #stage IN ({', '.join(names)})",
            ExpressionAttributeNames={"#stage": "stage"},
            ExpressionAttributeValues={":failed": "Failed", ":reason": reason, ":userId": job["userId"], **names},
        )
    except _conditional_failed():
        _log("fail skipped: job moved on", jobId=job_id)
        return
    logs.metric("INFO", "failed", {"JobsFailed": (1, "Count")}, stage=job["stage"], retryCount=retry_count,
                reason=reason)


def _synthesis_done(job):
    job_id, retry_count = _job_id(job), int(job.get("retryCount", 0))
    _put_event("SynthesisDone", {
        "eventId": f"synthesisdone-{job_id}-{retry_count}", "userId": job["userId"], "jobId": job_id,
        "tokenCount": int(job.get("tokenCount", 0)), "cost": float(job.get("cost", 0)),
    })


def _job_id(job):
    return job["PK"].removeprefix("JOB#")


def transcript_lines(segments):
    """"[mm:ss] text" per segment: the timestamps the templates' quotes refer to."""
    return [f"[{clock_time(s['start'])}] {s['text']}" for s in segments]


def clock_time(seconds):
    """mm:ss under an hour, h:mm:ss from there (the templates' timestamp pattern)."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


# --- DynamoDB ----------------------------------------------------------------------------------------


def _get(pk, sk):
    return _client("table").get_item(Key={"PK": pk, "SK": sk}).get("Item")


def _query(pk, sk_prefix):
    from boto3.dynamodb.conditions import Key

    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with(sk_prefix)}
    while True:
        page = _client("table").query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _schema(user_id, schema_id):
    """A built-in template, or one of the user's own. definition is the schema's raw text."""
    for pk in ("SCHEMA#BUILTIN", f"USER#{user_id}"):
        item = _get(pk, f"SCHEMA#{schema_id}")
        if item is not None:
            return item
    return None


# --- S3, SQS, EventBridge ----------------------------------------------------------------------------


def _read_bytes(bucket, key):
    """The object's bytes, or None if it isn't there. Without s3:ListBucket a missing key is 403, not 404, so
    403 also counts as "not there": a real permission problem shows up on the write that follows."""
    from botocore.exceptions import ClientError

    try:
        return _client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as error:
        if error.response["Error"]["Code"] in ("403", "404", "AccessDenied", "NoSuchKey", "NotFound"):
            return None
        raise


def _read_json(bucket, key):
    data = _read_bytes(bucket, key)
    return None if data is None else json.loads(data)


def _put_json(bucket, key, body):
    _client("s3").put_object(Bucket=bucket, Key=key, Body=json.dumps(body, default=str).encode("utf-8"),
                             ContentType="application/json")


def _saved(key, chunks_total):
    """A step's saved output, if it was made from this chunking of the recording."""
    saved = _read_json(UPLOADS_BUCKET, key)
    return saved if saved is not None and saved.get("chunksTotal") == chunks_total else None


def _release(record, seconds):
    """Make the message visible again after seconds (0: at once)."""
    try:
        _client("sqs").change_message_visibility(
            QueueUrl=SYNTHESIS_QUEUE_URL, ReceiptHandle=record["receiptHandle"], VisibilityTimeout=min(seconds, 900)
        )
    except Exception as error:  # it still comes back, when the queue's visibility timeout runs out
        _log("could not change visibility", messageId=record.get("messageId"), error=repr(error))


def _put_event(detail_type, detail):
    detail = {**detail, "occurredAt": events.occurred_at()}
    response = _client("events").put_events(Entries=[events.put_events_entry(detail_type, detail, BUS_NAME)])
    if response["FailedEntryCount"]:  # PutEvents doesn't raise on a partial failure
        raise RuntimeError(f"PutEvents failed: {response['Entries']}")


def _step(msg, step, started, **fields):
    """The line that closes a step, which is also its StepDuration metric (by Step and in total)."""
    duration = logs.ms_since(started)
    logs.metric("INFO", msg, {"StepDuration": (duration, "Milliseconds")}, {"Step": step}, durationMs=duration,
                **fields)


def _since(created_at):
    """Milliseconds from the job's createdAt (when the API pre-created it, before the upload) to now, or None."""
    try:
        created = datetime.datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        return None
    return round((datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() * 1000)
