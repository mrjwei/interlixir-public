"""Dead-letter handler: a pipeline message that used up its receives -> its job is Failed
with a reason the user can read, plus JobFailed for their history. The user's Retry then resumes the job from
what it already has, so the Job item, not the message, is what keeps a job from being lost.

One function, one SQS trigger per dead-letter queue (batch size 10, ReportBatchItemFailures). The queue a record
came from says what the message was:

  interlixir-upload-dlq          an S3 notification (recordings/...) or a RetryRequested event -> the job creator
                                   couldn't chunk it. The job must still be Queued (at the event's retryCount).
  interlixir-transcription-dlq   a TranscriptionChunk -> one chunk couldn't be transcribed. The job must still be
                                   Transcribing, with the message's chunksTotal (else it's from an older chunking).
  interlixir-synthesis-dlq       a SynthesisRequest -> the document couldn't be written. The job must still be
                                   Transcribing, Redacting or Synthesising.

Every write is conditional on the stage and retryCount it read, so a message for a job that has moved on (Done,
already Failed, retried, deleted) changes nothing. JobFailed goes out before the write, as in the job creator and
synthesis: a crash between the two re-emits the same eventId instead of losing it.

What it never does: retry the work itself, or put the message back. A message it can't tie to a job (not JSON, an
unknown shape, the job is gone) is logged in full and dropped; an AWS error leaves the record on its dead-letter
queue, which tries it again after the queue's 30 s visibility timeout.

Lambdas can't reach the cache, so the job status cache expires on its own TTL.
"""

import json
import logging
import os
import re
import traceback
import urllib.parse

from contracts import events, logs

logs.configure("dlq-handler")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "interlixir-uploads")
BUS_NAME = os.environ.get("BUS_NAME", "interlixir-bus")

ID = r"[A-Za-z0-9_-]{1,128}"
ID_PATTERN = re.compile(ID)
# The API's recording key (keys.recording_object_key), as the job creator matches it.
RECORDING_KEY = re.compile(rf"^recordings/({ID})/({ID})/({ID})\.(mp3|m4a|wav|webm|mp4)$")
JOB_SK = "META"
WORKING = ("Transcribing", "Redacting", "Synthesising")

REASONS = {
    "upload": "The recording couldn't be prepared for transcription after several attempts. Retry the job; "
              "if it fails again, upload the file again.",
    "transcription": "Part {part} of {parts} of the recording couldn't be transcribed after several attempts. "
                     "Retry the job: only the parts that are missing are transcribed again.",
    "synthesis": "The document couldn't be written after several attempts (the AI model may have been busy or "
                 "unavailable). Retry the job: the steps that already finished aren't repeated.",
}

_clients = {}


def _client(name):
    if name not in _clients:
        import boto3  # in the Lambda runtime; imported here so the tests can patch the clients
        from botocore.config import Config

        # Bounded: 3 attempts of at most 3 + 5 s each fit the function's 30 s timeout.
        config = Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard", "max_attempts": 3})
        if name == "table":
            _clients[name] = boto3.resource("dynamodb", region_name=REGION, config=config).Table(TABLE_NAME)
        else:
            _clients[name] = boto3.client(name, region_name=REGION, config=config)
    return _clients[name]


def _log(msg, level=logging.INFO, **fields):
    (logs.warning if level >= logging.WARNING else logs.info)(msg, **fields)


# --- entry point -------------------------------------------------------------------------------------


def handler(event, context):
    failures = []
    for record in event["Records"]:
        queue = record.get("eventSourceARN", "").rsplit(":", 1)[-1]
        fields = {"messageId": record["messageId"], "queue": queue}
        with logs.scope(requestId=getattr(context, "aws_request_id", None)):
            try:
                handle_record(queue, record["body"], fields)
            except Exception as error:
                # Stays on its dead-letter queue and comes back after the visibility timeout.
                logs.error("dead letter not handled, will be tried again", **fields, error=repr(error),
                           trace=traceback.format_exc())
                failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handle_record(queue, raw, fields):
    source = queue.removeprefix("interlixir-").removesuffix("-dlq")
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    targets = None
    if isinstance(body, dict):
        if source == "upload":
            targets = upload_targets(body)
        elif source == "transcription":
            targets = transcription_targets(body)
        elif source == "synthesis":
            targets = synthesis_targets(body)
    if targets is None:
        # Logged whole, so it can still be read after it's gone from the queue.
        _log("dropped: not a message this queue carries", logging.WARNING, **fields, body=raw[:4000])
        return
    for target in targets:
        mark_failed(source, target, fields)


# --- what each queue's message points at -------------------------------------------------------------
# Each returns a list of targets ({"jobId", "userId", and what the job must still look like}), [] when the
# message points at nothing that can fail, or None when it isn't a message of that queue at all.


def upload_targets(body):
    if body.get("Event") == "s3:TestEvent":
        return []
    if body.get("detail-type") == "RetryRequested" and body.get("source") == events.SOURCE:
        detail = body.get("detail") or {}
        if not _ids(detail, "jobId", "userId") or not isinstance(detail.get("retryCount"), int):
            return None
        return [{"jobId": detail["jobId"], "userId": detail["userId"], "stages": ("Queued",),
                 "retryCount": detail["retryCount"]}]
    if not isinstance(body.get("Records"), list):
        return None
    targets = []
    for record in body["Records"]:
        s3_info = record.get("s3", {}) if isinstance(record, dict) else {}
        key = urllib.parse.unquote_plus(s3_info.get("object", {}).get("key", ""))
        match = RECORDING_KEY.match(key)
        if s3_info.get("bucket", {}).get("name") != UPLOADS_BUCKET or match is None:
            continue  # the job creator ignores these too
        user_id, project_id, recording_id, _ = match.groups()
        recording = _get(f"PROJECT#{project_id}", f"REC#{recording_id}")
        if recording is None or recording.get("storageKey") != key or not ID_PATTERN.fullmatch(recording.get("jobId", "")):
            continue  # project deleted, or not an object the API presigned
        # Only the first run starts from an upload; a retried job's own message is a RetryRequested.
        targets.append({"jobId": recording["jobId"], "userId": user_id, "stages": ("Queued",), "retryCount": 0})
    return targets


def transcription_targets(body):
    if not _ids(body, "jobId", "userId") or not all(_count(body.get(n)) for n in ("chunk", "chunksTotal")):
        return None
    if not body["chunk"] < body["chunksTotal"]:
        return None
    return [{"jobId": body["jobId"], "userId": body["userId"], "stages": ("Transcribing",),
             "chunksTotal": body["chunksTotal"], "chunk": body["chunk"]}]


def synthesis_targets(body):
    if not _ids(body, "jobId", "userId"):
        return None
    return [{"jobId": body["jobId"], "userId": body["userId"], "stages": WORKING}]


def _ids(body, *names):
    return all(isinstance(body.get(n), str) and ID_PATTERN.fullmatch(body[n]) for n in names)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --- the job -----------------------------------------------------------------------------------------


def mark_failed(source, target, fields):
    job_id, user_id = target["jobId"], target["userId"]
    fields = {**fields, "jobId": job_id, "userId": user_id}
    job = _get(f"JOB#{job_id}", JOB_SK)
    if job is None or job.get("userId") != user_id:
        _log("skipped: job is gone", **fields)  # deleted with its project
        return
    stage, retry_count = job.get("stage"), int(job.get("retryCount", 0))
    stale = (
        stage not in target["stages"]
        or ("retryCount" in target and retry_count != target["retryCount"])
        or ("chunksTotal" in target and int(job.get("chunksTotal", -1)) != target["chunksTotal"])
    )
    if stale:
        _log("skipped: job moved on", **fields, stage=stage, retryCount=retry_count)
        return

    if source == "transcription":
        reason = REASONS[source].format(part=target["chunk"] + 1, parts=target["chunksTotal"])
    else:
        reason = REASONS[source]
    _put_event("JobFailed", {
        "eventId": f"jobfailed-{job_id}-{retry_count}", "userId": user_id, "jobId": job_id, "stage": stage,
    })
    try:
        _client("table").update_item(
            Key={"PK": f"JOB#{job_id}", "SK": JOB_SK},
            UpdateExpression="SET #stage = :failed, failureReason = :reason",
            ConditionExpression="userId = :userId AND #stage = :stage AND retryCount = :retryCount",
            ExpressionAttributeNames={"#stage": "stage"},
            ExpressionAttributeValues={
                ":failed": "Failed", ":reason": reason, ":userId": user_id, ":stage": stage,
                ":retryCount": retry_count,
            },
        )
    except _client("table").meta.client.exceptions.ConditionalCheckFailedException:
        _log("skipped: job moved on while being failed", **fields)
        return
    logs.metric("INFO", "job failed from dead letter", {"JobsFailed": (1, "Count")}, **fields, stage=stage,
                retryCount=retry_count, reason=reason)


# --- DynamoDB, EventBridge ---------------------------------------------------------------------------


def _get(pk, sk):
    return _client("table").get_item(Key={"PK": pk, "SK": sk}).get("Item")


def _put_event(detail_type, detail):
    detail = {**detail, "occurredAt": events.occurred_at()}
    response = _client("events").put_events(Entries=[events.put_events_entry(detail_type, detail, BUS_NAME)])
    if response["FailedEntryCount"]:  # PutEvents doesn't raise on a partial failure
        raise RuntimeError(f"PutEvents failed: {response['Entries']}")
