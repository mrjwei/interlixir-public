"""Whisper worker: one 5-minute chunk at a time from the transcription queue -> a transcript
in S3 -> once every chunk of a job is done, exactly one SynthesisRequest.

Per message (TranscriptionChunk, services/shared/contracts/messages.schema.json):
  task scale-in protection on -> visibility heartbeat on -> the Job still exists? -> transcript already in
  S3? (a redelivery) else download + transcribe + put -> ADD the chunk to doneChunks (a set, so a repeat can't
  double-count) -> if the set is complete, claim synthesisEnqueued; only the claimant sends SynthesisRequest and
  TranscriptionDone -> delete the message -> heartbeat off -> protection off.

Failures:
- an error leaves the message on the queue with a short backoff; after 3 receives it goes to the DLQ, where
  the dead-letter handler marks the job Failed
- SIGTERM (a deployment, a Spot interruption, a manual stop) during transcription abandons the chunk at the next
  segment and hands the message back at once, so another task picks it up; outside transcription the current
  step finishes first. Fargate gives a task stopTimeout (max 120 s) after SIGTERM.
- if ECS has already picked this task to stop (a scale-in caught it between chunks), turning protection on is
  refused with DEPLOYMENT_BLOCKED: the message is handed back untouched and the worker exits, rather than
  starting a chunk the coming SIGTERM would abandon.
- a stop is not an attempt: a message handed back on stop goes back as a new message (receive count 0), so only
  real failures count toward the DLQ (see hand_back()).
- the claim stores the winning message's ID, so if the winner dies after claiming, its own redelivery (same
  MessageId) can still send the request; any other message for the job can't. A copy handed back on stop carries
  that ID in its claimId attribute. Synthesis may therefore see a request twice and must be idempotent
  (shared/README.md).

The worker listens on nothing, never touches the cache, and logs one JSON object per line
(services/shared/contracts/logs.py). Every 60 s it also logs how long the task has been running, as the
WorkerTaskSeconds and FargateCost metrics: the dashboard's running-task count and the worker's share of
the estimated cost, without Container Insights on the shared cluster.
"""

import json
import logging
import os
import pathlib
import re
import signal
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

from contracts import events, logs
from transcriber import Stopped

logs.configure("whisper-worker")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "interlixir-uploads")
_QUEUES = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/"
TRANSCRIPTION_QUEUE_URL = os.environ.get("TRANSCRIPTION_QUEUE_URL", _QUEUES + "interlixir-transcription")
SYNTHESIS_QUEUE_URL = os.environ.get("SYNTHESIS_QUEUE_URL", _QUEUES + "interlixir-synthesis")
BUS_NAME = os.environ.get("BUS_NAME", "interlixir-bus")
WAIT_SECONDS = 20  # long poll
# While a chunk is in progress the heartbeat keeps the message hidden for VISIBILITY_SECONDS from "now", every
# HEARTBEAT_SECONDS. If the task dies, the chunk is visible again within VISIBILITY_SECONDS, whatever the
# queue's own (longer) default.
VISIBILITY_SECONDS = int(os.environ.get("VISIBILITY_SECONDS", "300"))
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "60"))
# Backoff before a failed chunk is tried again: this many seconds x the receive count.
RETRY_BACKOFF_SECONDS = 30
# Message attribute on a copy handed back on stop: the first message's ID, which the G8 claim is keyed on.
CLAIM_ATTRIBUTE = "claimId"
# Protection lapses on its own after this, in case a task hangs while holding it.
PROTECTION_MINUTES = 60
JOB_SK = "META"
# USD per task-hour: 1 vCPU x 0.03885 + 2 GB x 0.00426, Fargate Linux/ARM in ap-southeast-2 (AWS price list,
# 2026-10-19). Change it with the task size.
FARGATE_PRICE_PER_HOUR = float(os.environ.get("FARGATE_PRICE_PER_HOUR", "0.04737"))
METER_SECONDS = 60

ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

stopping = threading.Event()
_clients = {}


def _client(name):
    if name not in _clients:
        import boto3
        from botocore.config import Config

        # read_timeout is above the long poll's 20 s.
        config = Config(connect_timeout=5, read_timeout=60, retries={"mode": "standard", "max_attempts": 5})
        if name == "table":
            _clients[name] = boto3.resource("dynamodb", region_name=REGION, config=config).Table(TABLE_NAME)
        else:
            _clients[name] = boto3.client(name, region_name=REGION, config=config)
    return _clients[name]


def _log(msg, level=logging.INFO, **fields):
    if level >= logging.ERROR:
        logs.error(msg, **fields)
    elif level >= logging.WARNING:
        logs.warning(msg, **fields)
    else:
        logs.info(msg, **fields)


def _conditional_failed():
    return _client("table").meta.client.exceptions.ConditionalCheckFailedException


# --- keys (layout in the deployment notes) --------------------------------------------------------------


def chunk_key(job_id, n):
    return f"chunks/{job_id}/{n}.wav"


def transcript_key(user_id, job_id, n):
    return f"transcripts/{user_id}/{job_id}/chunks/{n}.json"


# --- the loop ----------------------------------------------------------------------------------------


def main():
    meter = Meter()  # from the start, so the model load counts too
    meter.start()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    from transcriber import Transcriber

    transcriber = Transcriber()
    protection = Protection()
    _log(
        "ready", model=transcriber.name, beamSize=transcriber.beam_size, cpuThreads=transcriber.cpu_threads,
        loadMs=round(transcriber.load_seconds * 1000),
    )
    while not stopping.is_set():
        try:
            poll_once(transcriber, protection)
        except Exception as error:  # e.g. SQS unreachable: whatever was in flight comes back on its own
            _log("poll failed", logging.ERROR, error=repr(error), trace=traceback.format_exc())
            time.sleep(5)
    _log("stopped")
    meter.stop()


def _on_signal(signum, frame):
    _log("stop requested", signal=signal.Signals(signum).name)
    stopping.set()


def poll_once(transcriber, protection):
    response = _client("sqs").receive_message(
        QueueUrl=TRANSCRIPTION_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=WAIT_SECONDS,
        MessageSystemAttributeNames=["ApproximateReceiveCount"],
        MessageAttributeNames=[CLAIM_ATTRIBUTE],
    )
    for message in response.get("Messages", []):
        if stopping.is_set():  # SIGTERM arrived during the long poll
            _log("released on stop, not started", messageId=message["MessageId"], newMessageId=hand_back(message))
            continue
        process(message, transcriber, protection)


def process(message, transcriber, protection):
    handle, message_id = message["ReceiptHandle"], message["MessageId"]
    receives = int(message.get("Attributes", {}).get("ApproximateReceiveCount", 1))
    with logs.scope(messageId=message_id, receiveCount=receives):
        _process(message, handle, receives, transcriber, protection)


def _process(message, handle, receives, transcriber, protection):
    if not protection.set(True):  # ECS is about to stop this task; SIGTERM follows
        _log("released, task is being stopped", newMessageId=hand_back(message))
        stopping.set()
        return
    heartbeat = Heartbeat(handle)
    heartbeat.start()
    outcome = None
    try:
        body = parse(message["Body"])
        logs.bind(jobId=body["jobId"], userId=body["userId"], chunk=body["chunk"], stage="Transcribing")
        handle_chunk(body, claim_id(message), transcriber)
        outcome = "delete"
    except Stopped:
        outcome = "release"
    except Exception as error:
        _log("chunk failed, will be retried", logging.ERROR, error=repr(error), trace=traceback.format_exc())
        outcome = "backoff"
    finally:
        heartbeat.stop()
        # The heartbeat has stopped, so nothing changes the message after this.
        try:
            if outcome == "delete":
                _client("sqs").delete_message(QueueUrl=TRANSCRIPTION_QUEUE_URL, ReceiptHandle=handle)
            elif outcome == "release":
                _log("released on stop, chunk abandoned", newMessageId=hand_back(message))
            elif outcome == "backoff":
                release(handle, min(VISIBILITY_SECONDS, RETRY_BACKOFF_SECONDS * receives))
        finally:
            protection.set(False)


def claim_id(message):
    """The ID the G8 claim is keyed on: the first message's, also on a copy handed back on stop."""
    attribute = message.get("MessageAttributes", {}).get(CLAIM_ATTRIBUTE)
    return attribute["StringValue"] if attribute else message["MessageId"]


def hand_back(message):
    """Returns a message this task won't work on because it's stopping, and the new message's ID (None if it was
    released instead). A stop isn't the chunk's fault, but every receive counts toward the queue's 3 before the
    DLQ, and a step-down stops several tasks at once: in a load test one message was refused by three
    stopping tasks in a row and its job failed without a single attempt. So the body goes back as a new message
    (receive count 0), carrying the claim ID, and the received one is deleted. If the send fails, the message is
    released as before. If the delete fails, the chunk is queued twice, which the transcript check and the
    doneChunks set already absorb."""
    try:
        new_id = _client("sqs").send_message(
            QueueUrl=TRANSCRIPTION_QUEUE_URL, MessageBody=message["Body"],
            MessageAttributes={CLAIM_ATTRIBUTE: {"DataType": "String", "StringValue": claim_id(message)}},
        )["MessageId"]
    except Exception as error:
        _log("could not hand back as a new message, released instead", logging.WARNING, error=repr(error))
        release(message["ReceiptHandle"], 0)
        return None
    try:
        _client("sqs").delete_message(QueueUrl=TRANSCRIPTION_QUEUE_URL, ReceiptHandle=message["ReceiptHandle"])
    except Exception as error:
        _log("could not delete a handed-back message", logging.WARNING, error=repr(error))
    return new_id


def release(handle, seconds):
    try:
        _client("sqs").change_message_visibility(
            QueueUrl=TRANSCRIPTION_QUEUE_URL, ReceiptHandle=handle, VisibilityTimeout=seconds
        )
    except Exception as error:  # the message still comes back, when its current timeout runs out
        _log("could not change visibility", logging.WARNING, error=repr(error))


def parse(raw):
    """A TranscriptionChunk body, checked enough to build keys from it. A bad body raises, so it ends in the DLQ
    where it can be read, rather than being dropped."""
    body = json.loads(raw)
    for name in ("jobId", "userId"):
        if not isinstance(body.get(name), str) or not ID.fullmatch(body[name]):
            raise ValueError(f"bad {name} in message")
    for name in ("chunk", "chunksTotal", "startSeconds"):
        if not isinstance(body.get(name), int) or isinstance(body[name], bool) or body[name] < 0:
            raise ValueError(f"bad {name} in message")
    if not body["chunk"] < body["chunksTotal"]:
        raise ValueError("chunk is not below chunksTotal")
    return body


# --- one chunk ---------------------------------------------------------------------------------------


def handle_chunk(body, message_id, transcriber):
    job_id, user_id, n, total = body["jobId"], body["userId"], body["chunk"], body["chunksTotal"]
    fields = {"jobId": job_id, "chunk": n, "chunksTotal": total}

    job = _client("table").get_item(Key={"PK": f"JOB#{job_id}", "SK": JOB_SK}).get("Item")
    if job is None or job.get("userId") != user_id:
        _log("skipped: job is gone", **fields)  # deleted with its project; nothing to do
        return
    if int(job.get("chunksTotal", -1)) != total:
        # The job creator writes chunksTotal before sending any chunk, so this message is from an older run.
        _log("skipped: job was chunked again", **fields, jobChunksTotal=job.get("chunksTotal"))
        return

    key = transcript_key(user_id, job_id, n)
    if _exists(UPLOADS_BUCKET, key):
        _log("transcript already written", **fields)  # a redelivery, or the job creator sent the chunk twice
    else:
        transcribe_chunk(body, key, transcriber, fields)

    record_done(job_id, user_id, n, total, message_id, fields)


def transcribe_chunk(body, key, transcriber, fields):
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "chunk.wav"
        _client("s3").download_file(UPLOADS_BUCKET, chunk_key(body["jobId"], body["chunk"]), str(path))
        downloaded = time.monotonic()
        result = transcriber.transcribe(path, body["startSeconds"], should_stop=stopping.is_set)
    transcribed = time.monotonic()

    document = {
        "jobId": body["jobId"],
        "chunk": body["chunk"],
        "startSeconds": body["startSeconds"],
        "model": transcriber.name,
        "language": "en",
        **result,
    }
    _client("s3").put_object(
        Bucket=UPLOADS_BUCKET, Key=key, Body=json.dumps(document).encode(), ContentType="application/json"
    )
    seconds = transcribed - downloaded
    duration = _ms(started, time.monotonic())
    logs.metric(
        "INFO", "transcribed", {"StepDuration": (duration, "Milliseconds")}, {"Step": "transcribe"}, **fields,
        audioSeconds=result["audioSeconds"], segments=len(result["segments"]),
        downloadMs=_ms(started, downloaded), transcribeMs=round(seconds * 1000),
        rtf=round(seconds / result["audioSeconds"], 3) if result["audioSeconds"] else None, durationMs=duration,
    )


def record_done(job_id, user_id, n, total, message_id, fields):
    """G8. ADD to a set is idempotent, so the set's size is the number of distinct chunks done. The condition
    keeps a late message from recreating a deleted Job (an ADD on a missing item creates it) or counting toward
    a job that was chunked again with a different total. ALL_NEW rather than UPDATED_NEW: when the chunk was
    already in the set nothing changes, and the set must still come back for the check below."""
    try:
        done = _client("table").update_item(
            Key={"PK": f"JOB#{job_id}", "SK": JOB_SK},
            UpdateExpression="ADD doneChunks :n",
            ConditionExpression="userId = :userId AND chunksTotal = :total",
            ExpressionAttributeValues={":n": {n}, ":userId": user_id, ":total": total},
            ReturnValues="ALL_NEW",
        )["Attributes"]["doneChunks"]
    except _conditional_failed():
        _log("skipped: job is gone or was chunked again", **fields)
        return
    _log("chunk recorded", **fields, chunksDone=len(done))
    if len(done) < total:
        return

    # Every chunk is in: claim the synthesis request. The value is this message's ID, so this message's own
    # redelivery can finish a claim it made before dying; a different message can't claim it again.
    try:
        job = _client("table").update_item(
            Key={"PK": f"JOB#{job_id}", "SK": JOB_SK},
            UpdateExpression="SET synthesisEnqueued = :claim",
            ConditionExpression="userId = :userId AND (attribute_not_exists(synthesisEnqueued) OR synthesisEnqueued = :claim)",
            ExpressionAttributeValues={":claim": message_id, ":userId": user_id},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except _conditional_failed():
        _log("synthesis already requested", **fields)
        return
    _send(SYNTHESIS_QUEUE_URL, {"jobId": job_id, "userId": user_id})
    retry_count = int(job.get("retryCount", 0))
    _put_event(
        "TranscriptionDone",
        {"eventId": f"transcriptiondone-{job_id}-{retry_count}", "userId": user_id, "jobId": job_id},
    )
    _log("synthesis requested", **fields, retryCount=retry_count)


# --- G4: task scale-in protection --------------------------------------------------------------------


class Protection:
    """ECS task scale-in protection through the ECS agent's endpoint, on while a chunk is in progress, so a
    scale-in only stops idle tasks. ECS_AGENT_URI is only set inside a task (Fargate platform 1.4+); without it
    this does nothing. If the task role may not call ecs:UpdateTaskProtection, it logs that
    once and carries on without: SIGTERM handling and SQS redelivery still keep chunks from being lost.

    set() returns False only when ECS refuses because it has already picked this task to stop (DEPLOYMENT_BLOCKED:
    a scale-in or deployment caught it unprotected between chunks). Every other outcome returns True, so the
    worker carries on as it would without protection."""

    def __init__(self, agent_uri=None):
        self.url = f"{agent_uri or os.environ.get('ECS_AGENT_URI', '')}/task-protection/v1/state"
        self.available = bool(agent_uri or os.environ.get("ECS_AGENT_URI"))
        self.confirmed = False

    def set(self, enabled):
        if not self.available:
            return True
        body = {"ProtectionEnabled": enabled, **({"ExpiresInMinutes": PROTECTION_MINUTES} if enabled else {})}
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="PUT", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                result = {"status": error.code, "body": error.read().decode(errors="replace")}
        except Exception as error:  # the agent didn't answer: try again next chunk
            _log("task protection call failed", logging.WARNING, enabled=enabled, error=repr(error))
            return True
        if "protection" in result:
            if not self.confirmed:
                _log("task protection works", protection=result["protection"])
                self.confirmed = True
            return True
        if "AccessDenied" in json.dumps(result):
            self.available = False
            _log("task protection not permitted, continuing without it", logging.WARNING, result=result)
            return True
        _log("task protection refused", logging.WARNING, enabled=enabled, result=result)
        return not (enabled and (result.get("failure") or {}).get("Reason") == "DEPLOYMENT_BLOCKED")


# --- visibility heartbeat ----------------------------------------------------------------------------


class Heartbeat:
    """Keeps the message hidden while a chunk is in progress, however long transcription takes."""

    def __init__(self, handle):
        self.handle = handle
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._done.set()
        self._thread.join()

    def _run(self):
        while not self._done.wait(HEARTBEAT_SECONDS):
            release(self.handle, VISIBILITY_SECONDS)


# --- task time meter ----------------------------------------------------------------------------


class Meter:
    """Logs the seconds since its last line every METER_SECONDS, and once more on stop, as WorkerTaskSeconds and
    FargateCost (USD). A 1-minute Sum of WorkerTaskSeconds / 60 is the number of running tasks. Not counted:
    the ~20 s before the process starts (image pull, ENI), and nothing after a crash's last line."""

    def __init__(self):
        self.last = time.monotonic()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._done.set()
        self._thread.join()
        self.emit()

    def _run(self):
        while not self._done.wait(METER_SECONDS):
            self.emit()

    def emit(self):
        now = time.monotonic()
        seconds, self.last = now - self.last, now
        # bound=False: the meter runs beside the chunk, and isn't part of its trace.
        logs.metric(
            "INFO", "task time",
            {"WorkerTaskSeconds": (round(seconds, 1), "Seconds"),
             "FargateCost": (round(seconds * FARGATE_PRICE_PER_HOUR / 3600, 8), "None")},
            bound=False,
        )


# --- S3, SQS, EventBridge ----------------------------------------------------------------------------


def _exists(bucket, key):
    """HEAD the object. Without s3:ListBucket, S3 answers a missing key with 403 rather than 404, so 403 also
    counts as "not there": if the role really can't read the bucket, the download right after fails anyway."""
    from botocore.exceptions import ClientError

    try:
        _client("s3").head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] in ("403", "404", "NoSuchKey", "NotFound"):
            return False
        raise


def _send(queue_url, body):
    _client("sqs").send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))


def _put_event(detail_type, detail):
    detail = {**detail, "occurredAt": events.occurred_at()}
    response = _client("events").put_events(Entries=[events.put_events_entry(detail_type, detail, BUS_NAME)])
    if response["FailedEntryCount"]:  # PutEvents doesn't raise on a partial failure
        raise RuntimeError(f"PutEvents failed: {response['Entries']}")


def _ms(start, end):
    return round((end - start) * 1000)


if __name__ == "__main__":
    main()
