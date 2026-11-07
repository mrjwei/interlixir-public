"""Job creator: a recording in the uploads bucket -> 5-minute chunks on the transcription queue.

Everything arrives through the upload queue interlixir-upload (event source mapping, batch size 1):
- an S3 notification (ObjectCreated:Post on recordings/) starts a job the API pre-created
- a RetryRequested event (EventBridge rule -> the same queue) resumes a job from its earliest missing artifact
A RetryRequested invoked straight from an EventBridge rule (Lambda target) is handled too.

Ordering, on both paths. The job stays Queued until the last step, and every step before it is safe to repeat:
  chunk objects (deterministic keys) -> "prepare" write (chunksTotal, doneChunks reset) -> queue messages ->
  UploadReceived (deterministic eventId) -> "commit" write: stage Transcribing.
Both writes are conditional on the job still being Queued at the retryCount this run started from. A failure
anywhere leaves the job Queued, the message comes back, and the whole thing is redone; a duplicate chunk message
only costs a worker some time, because transcripts have deterministic keys and doneChunks is a set. A
duplicate S3 event that arrives after the commit sees the job is no longer Queued and does nothing.

Lambdas can't reach the cache, so the job status cache expires on its own TTL.
"""

import json
import os
import pathlib
import re
import tempfile
import time
import traceback
import urllib.parse

import media
from contracts import events, logs

logs.configure("job-creator")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "interlixir-uploads")
_QUEUES = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/"
TRANSCRIPTION_QUEUE_URL = os.environ.get("TRANSCRIPTION_QUEUE_URL", _QUEUES + "interlixir-transcription")
SYNTHESIS_QUEUE_URL = os.environ.get("SYNTHESIS_QUEUE_URL", _QUEUES + "interlixir-synthesis")
BUS_NAME = os.environ.get("BUS_NAME", "interlixir-bus")
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", "300"))
# Bounds /tmp and the function's run time (sized for this), and what one upload can cost in worker time.
MAX_AUDIO_SECONDS = int(os.environ.get("MAX_AUDIO_SECONDS", str(3 * 3600)))

ID = r"[A-Za-z0-9_-]{1,128}"
# Built by the API (keys.recording_object_key) from the verified userId; the extensions are the API's
# RECORDING_TYPES.
RECORDING_KEY = re.compile(rf"^recordings/({ID})/({ID})/({ID})\.(mp3|m4a|wav|webm|mp4)$")
JOB_SK = "META"
SQS_BATCH = 10
PROBE_TIMEOUT = 60
# Left for uploading the chunks and the writes after ffmpeg, out of the function's timeout.
AFTER_SPLIT_SECONDS = 60

_clients = {}


def _client(name):
    if name not in _clients:
        import boto3  # in the Lambda runtime; imported here so media tests don't need it
        from botocore.config import Config

        config = Config(connect_timeout=5, read_timeout=60, retries={"mode": "standard", "max_attempts": 5})
        if name == "table":
            _clients[name] = boto3.resource("dynamodb", region_name=REGION, config=config).Table(TABLE_NAME)
        else:
            _clients[name] = boto3.client(name, region_name=REGION, config=config)
    return _clients[name]


def _log(msg, **fields):
    logs.info(msg, **fields)


# --- keys (layout in the deployment notes) --------------------------------------------------------------


def chunk_key(job_id, n):
    return f"chunks/{job_id}/{n}.wav"


def transcripts_prefix(user_id, job_id):
    return f"transcripts/{user_id}/{job_id}/chunks/"


def _job_id(job):
    return job["PK"].removeprefix("JOB#")


# --- entry point -------------------------------------------------------------------------------------


def handler(event, context):
    if event.get("detail-type") == "RetryRequested":  # an EventBridge rule with this function as its target
        with logs.scope(requestId=getattr(context, "aws_request_id", None)):
            resume(event["detail"], context)
        return {"resumed": event["detail"]["jobId"]}

    failures = []
    for record in event["Records"]:
        with logs.scope(requestId=getattr(context, "aws_request_id", None), messageId=record["messageId"]):
            try:
                handle_message(json.loads(record["body"]), context)
            except Exception as error:
                # Only this message comes back (ReportBatchItemFailures); after 3 receives it goes to the DLQ.
                logs.error("message failed, will be redelivered", error=repr(error), trace=traceback.format_exc())
                failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handle_message(body, context):
    if body.get("Event") == "s3:TestEvent":  # sent once when the notification is saved; has no Records
        _log("ignored s3:TestEvent")
        return
    if body.get("detail-type") == "RetryRequested" and body.get("source") == events.SOURCE:
        resume(body["detail"], context)
        return
    if "Records" not in body:
        _log("ignored unknown message", keys=sorted(body))
        return
    for record in body["Records"]:
        start(record, context)


# --- upload: start a pre-created job ------------------------------------------------------------


def start(record, context):
    name = record.get("eventName", "")
    s3_info = record.get("s3", {})
    bucket = s3_info.get("bucket", {}).get("name")
    # Keys in S3 events are URL-encoded, with spaces as '+'.
    key = urllib.parse.unquote_plus(s3_info.get("object", {}).get("key", ""))
    size = s3_info.get("object", {}).get("size", 0)
    if not name.startswith("ObjectCreated:") or bucket != UPLOADS_BUCKET:
        _log("ignored record", eventName=name, bucket=bucket)
        return
    if key.endswith("/") or size == 0:  # a console "folder" marker or an empty object
        _log("ignored empty object or folder", key=key, size=size)
        return
    match = RECORDING_KEY.match(key)
    if match is None:
        _log("ignored key that isn't a recording", key=key)
        return
    user_id, project_id, recording_id, _ = match.groups()

    recording = _get(f"PROJECT#{project_id}", f"REC#{recording_id}")
    if recording is None or recording.get("storageKey") != key:
        # The project was deleted before the file arrived, or the object wasn't presigned by the API.
        _log("ignored: no Recording item for this key", key=key)
        return
    job = _get(f"JOB#{recording['jobId']}", JOB_SK)
    logs.bind(jobId=recording["jobId"], userId=user_id, stage=(job or {}).get("stage"))
    if job is None or job.get("userId") != user_id:
        _log("ignored: no matching Job", key=key, jobId=recording["jobId"])
        return
    # Not "chunksTotal is set": the prepare write sets it before the messages go out, so a redelivery after a
    # failure between the two must still get through. Queued at retryCount 0 is only true until the commit.
    if job.get("stage") != "Queued" or int(job.get("retryCount", 0)) != 0:
        _log("ignored duplicate upload event", jobId=_job_id(job), stage=job.get("stage"))
        return
    chunk_and_enqueue(job, key, context, first_upload=True)


# --- RetryRequested: resume from the earliest missing artifact ---------------------------------------


def resume(detail, context):
    job_id, user_id, retry_count = detail["jobId"], detail["userId"], int(detail["retryCount"])
    logs.bind(jobId=job_id, userId=user_id, retryCount=retry_count)
    job = _get(f"JOB#{job_id}", JOB_SK)
    logs.bind(stage=(job or {}).get("stage"))
    if job is None or job.get("userId") != user_id:
        _log("ignored retry: job is gone", jobId=job_id)  # deleted with its project
        return
    if job.get("stage") != "Queued" or int(job.get("retryCount", 0)) != retry_count:
        _log("ignored stale retry", jobId=job_id, stage=job.get("stage"), retryCount=retry_count)
        return

    if "chunksTotal" in job:
        total = int(job["chunksTotal"])
        done = _chunk_numbers(UPLOADS_BUCKET, transcripts_prefix(user_id, job_id), ".json")
        missing = [n for n in range(total) if n not in done]
        if not missing:
            # Every transcript is there, so the failure was in synthesis: go straight back to it.
            if _prepare(job, {"doneChunks": set(range(total)), "synthesisEnqueued": True}, ["failureReason"]):
                _send(SYNTHESIS_QUEUE_URL, [{"jobId": job_id, "userId": user_id}])
                _commit(job)
                _log("retry: synthesis requested", jobId=job_id, chunksTotal=total)
            return
        if set(missing) <= _chunk_numbers(UPLOADS_BUCKET, f"chunks/{job_id}/", ".wav"):
            # doneChunks is reset to what's really in S3, so the G8 count can reach chunksTotal again.
            values = {"doneChunks": done} if done else {}
            remove = ["synthesisEnqueued", "failureReason"] + ([] if done else ["doneChunks"])
            if _prepare(job, values, remove):
                _send(TRANSCRIPTION_QUEUE_URL, [_chunk_message(job, n, total) for n in missing])
                _commit(job)
                _log("retry: chunks re-enqueued", jobId=job_id, chunks=missing, chunksTotal=total)
            return

    # Never chunked (it failed while Queued), or chunk audio is missing: start again from the recording.
    project_id, recording_id = detail["projectId"], detail["recordingId"]
    recording = _get(f"PROJECT#{project_id}", f"REC#{recording_id}")
    key = (recording or {}).get("storageKey", "")
    if (
        recording is None
        or recording.get("jobId") != job_id
        or not key.startswith(f"recordings/{user_id}/{project_id}/")
        or not _exists(UPLOADS_BUCKET, key)
    ):
        fail(job, "The original recording is no longer stored, so this job can't be retried. Upload the file again.")
        return
    chunk_and_enqueue(job, key, context, first_upload=False)


# --- the work ----------------------------------------------------------------------------------------


def chunk_and_enqueue(job, key, context, first_upload):
    job_id, user_id = _job_id(job), job["userId"]
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:  # /tmp survives between invocations on a warm instance
        source = pathlib.Path(tmp) / "input"
        _client("s3").download_file(UPLOADS_BUCKET, key, str(source))
        input_bytes = source.stat().st_size
        downloaded = time.monotonic()
        try:
            seconds = media.probe(source, PROBE_TIMEOUT)
            if seconds is not None and seconds > MAX_AUDIO_SECONDS:
                raise media.InputError(f"The recording is longer than {MAX_AUDIO_SECONDS // 3600} hours, the limit.")
            chunks = media.split(source, pathlib.Path(tmp) / "chunks", CHUNK_SECONDS, _split_timeout(context))
        except media.InputError as error:
            fail(job, str(error))
            return
        if seconds is None and len(chunks) * CHUNK_SECONDS > MAX_AUDIO_SECONDS + CHUNK_SECONDS:
            fail(job, f"The recording is longer than {MAX_AUDIO_SECONDS // 3600} hours, the limit.")
            return
        split_done = time.monotonic()
        for n, path in enumerate(chunks):
            _client("s3").upload_file(str(path), UPLOADS_BUCKET, chunk_key(job_id, n), ExtraArgs={"ContentType": "audio/wav"})
    uploaded = time.monotonic()

    total = len(chunks)
    values = {"chunksTotal": total}
    if seconds is not None:
        values["audioSeconds"] = round(seconds)
    if not _prepare(job, values, ["doneChunks", "synthesisEnqueued", "failureReason"]):
        return
    _send(TRANSCRIPTION_QUEUE_URL, [_chunk_message(job, n, total) for n in range(total)])
    if first_upload:
        _put_event("UploadReceived", {"eventId": f"uploadreceived-{job_id}", "userId": user_id, "jobId": job_id})
    _commit(job)
    duration = _ms(started, time.monotonic())
    logs.metric(
        "INFO", "chunked", {"StepDuration": (duration, "Milliseconds")}, {"Step": "chunk"},
        jobId=job_id, retryCount=int(job.get("retryCount", 0)), chunks=total, audioSeconds=seconds,
        inputBytes=input_bytes, downloadMs=_ms(started, downloaded), splitMs=_ms(downloaded, split_done),
        uploadMs=_ms(split_done, uploaded), durationMs=duration,
    )


def fail(job, reason):
    """The input can't be processed: Failed with a reason the user sees, plus JobFailed for their history.
    The event goes first, so a crash between the two re-emits it (same eventId) rather than losing it."""
    job_id, retry_count = _job_id(job), int(job.get("retryCount", 0))
    _put_event(
        "JobFailed",
        {"eventId": f"jobfailed-{job_id}-{retry_count}", "userId": job["userId"], "jobId": job_id, "stage": "Queued"},
    )
    try:
        _client("table").update_item(
            Key={"PK": job["PK"], "SK": JOB_SK},
            UpdateExpression="SET #stage = :failed, failureReason = :reason",
            **_while_queued(job, {":failed": "Failed", ":reason": reason}),
        )
    except _client("table").meta.client.exceptions.ConditionalCheckFailedException:
        _log("fail skipped: job moved on", jobId=job_id)
        return
    logs.metric("INFO", "failed", {"JobsFailed": (1, "Count")}, jobId=job_id, retryCount=retry_count, reason=reason)


# --- DynamoDB ----------------------------------------------------------------------------------------


def _get(pk, sk):
    return _client("table").get_item(Key={"PK": pk, "SK": sk}).get("Item")


def _while_queued(job, values, names=None):
    """Condition for every job write here: still Queued, still this user's, at the retryCount this run read."""
    return {
        "ConditionExpression": "#stage = :queued AND userId = :userId AND retryCount = :retryCount",
        "ExpressionAttributeNames": {"#stage": "stage", **(names or {})},
        "ExpressionAttributeValues": {
            ":queued": "Queued", ":userId": job["userId"], ":retryCount": int(job.get("retryCount", 0)), **values,
        },
    }


def _prepare(job, values, remove):
    """The writes that must land before any chunk message is sent: chunksTotal (so the G8 count has a total)
    and the doneChunks reset (so it can't overwrite a worker's ADD). False if the job is no longer Queued."""
    names, placeholders, sets = {}, {}, []
    for i, (name, value) in enumerate(values.items()):
        names[f"#f{i}"], placeholders[f":v{i}"] = name, value
        sets.append(f"#f{i} = :v{i}")
    expression = " ".join(part for part in (
        "SET " + ", ".join(sets) if sets else "",
        "REMOVE " + ", ".join(remove) if remove else "",
    ) if part)
    try:
        _client("table").update_item(
            Key={"PK": job["PK"], "SK": JOB_SK}, UpdateExpression=expression, **_while_queued(job, placeholders, names)
        )
        return True
    except _client("table").meta.client.exceptions.ConditionalCheckFailedException:
        _log("skipped: job is no longer Queued", jobId=_job_id(job))
        return False


def _commit(job):
    try:
        _client("table").update_item(
            Key={"PK": job["PK"], "SK": JOB_SK},
            UpdateExpression="SET #stage = :transcribing",
            **_while_queued(job, {":transcribing": "Transcribing"}),
        )
    except _client("table").meta.client.exceptions.ConditionalCheckFailedException:
        _log("commit skipped: a concurrent duplicate committed first", jobId=_job_id(job))


# --- S3, SQS, EventBridge ----------------------------------------------------------------------------


def _chunk_numbers(bucket, prefix, suffix):
    """{n} for the objects <prefix><n><suffix>."""
    found = set()
    for page in _client("s3").get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            stem = obj["Key"][len(prefix) :].removesuffix(suffix)
            if obj["Key"].endswith(suffix) and stem.isdigit():
                found.add(int(stem))
    return found


def _exists(bucket, key):
    from botocore.exceptions import ClientError

    try:
        _client("s3").head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _chunk_message(job, n, total):
    """TranscriptionChunk (services/shared/contracts/messages.schema.json)."""
    return {"jobId": _job_id(job), "userId": job["userId"], "chunk": n, "chunksTotal": total, "startSeconds": n * CHUNK_SECONDS}


def _send(queue_url, bodies):
    for start in range(0, len(bodies), SQS_BATCH):
        batch = bodies[start : start + SQS_BATCH]
        entries = [{"Id": str(i), "MessageBody": json.dumps(body)} for i, body in enumerate(batch)]
        response = _client("sqs").send_message_batch(QueueUrl=queue_url, Entries=entries)
        if response.get("Failed"):
            # Raising redoes the whole run; the entries that did go through are sent again, which is harmless.
            raise RuntimeError(f"SendMessageBatch failed for {response['Failed']}")


def _put_event(detail_type, detail):
    detail = {**detail, "occurredAt": events.occurred_at()}
    response = _client("events").put_events(Entries=[events.put_events_entry(detail_type, detail, BUS_NAME)])
    if response["FailedEntryCount"]:  # PutEvents doesn't raise on a partial failure
        raise RuntimeError(f"PutEvents failed: {response['Entries']}")


# --- time --------------------------------------------------------------------------------------------


def _split_timeout(context):
    left = context.get_remaining_time_in_millis() / 1000 if context else 600
    return max(10, left - AFTER_SPLIT_SECONDS)


def _ms(start, end):
    return round((end - start) * 1000)
