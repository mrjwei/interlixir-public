"""Stream router: DynamoDB stream records -> control events on the A3 bus.

The event source mapping's filter criteria (filters.json) only pass two kinds of record:
- REMOVE of a Project (SK PROJECT#) or Document (SK DOC#) item -> DeleteRequested (a Document's names its jobId and
  recordingId too, from the old image)
- MODIFY of a Job whose stage goes Failed -> Queued -> RetryRequested

It's a pure translator: no table reads. eventId is the stream record's eventID and occurredAt its
ApproximateCreationDateTime, so a retried batch puts identical events and consumers dedupe on eventId.
GC's own deletes come back here as more REMOVE records; GC treats them as no-ops.
"""

import datetime
import os

from contracts import events, logs

logs.configure("stream-router")

BUS_NAME = os.environ.get("BUS_NAME", "interlixir-bus")
PUT_EVENTS_MAX_ENTRIES = 10

_client = None


def _events_client():
    global _client
    if _client is None:
        import boto3  # in the Lambda runtime; imported here so the unit tests don't need it
        from botocore.config import Config

        # Bounded: a hung call fails within the function's 30 s timeout and the mapping retries the batch.
        _client = boto3.client("events", config=Config(
            connect_timeout=3, read_timeout=5, retries={"mode": "standard", "max_attempts": 3}))
    return _client


def _strip(value, prefix):
    if not value.startswith(prefix):
        raise ValueError(f"expected {prefix}... got {value!r}")
    return value[len(prefix) :]


def _attr(image, name, kind="S"):
    """A typed attribute from a stream image, or None if it's missing."""
    return image.get(name, {}).get(kind)


def _common(record):
    created = datetime.datetime.fromtimestamp(record["dynamodb"]["ApproximateCreationDateTime"], datetime.timezone.utc)
    return {"eventId": record["eventID"], "occurredAt": events.occurred_at(created)}


def to_event(record):
    """(detail-type, detail) for one stream record, or None if it isn't one the router handles."""
    change = record["dynamodb"]
    if record["eventName"] == "REMOVE":
        old = change["OldImage"]
        pk, sk = _attr(old, "PK"), _attr(old, "SK")
        if sk.startswith("PROJECT#"):
            detail = {"userId": _strip(pk, "USER#"), "entity": "Project", "projectId": _strip(sk, "PROJECT#")}
        elif sk.startswith("DOC#"):
            user_id = _attr(old, "userId")
            if user_id is None:
                # Written by synthesis from M8 on. Without it GC can't find the S3 object; a retry won't help.
                logs.error("document without userId, skipped", PK=pk, SK=sk)
                return None
            detail = {
                "userId": user_id,
                "entity": "Document",
                "projectId": _strip(pk, "PROJECT#"),
                "documentId": _strip(sk, "DOC#"),
            }
            # What GC removes with the document: its job, and the recording the API deleted alongside it.
            for name in ("jobId", "recordingId"):
                if _attr(old, name) is not None:
                    detail[name] = _attr(old, name)
        else:
            return None
        return "DeleteRequested", {**_common(record), **detail}

    if record["eventName"] == "MODIFY":
        old, new = change["OldImage"], change["NewImage"]
        if _attr(old, "stage") != "Failed" or _attr(new, "stage") != "Queued":
            return None
        detail = {
            "userId": _attr(new, "userId"),
            "jobId": _strip(_attr(new, "PK"), "JOB#"),
            "projectId": _strip(_attr(new, "GSI1PK"), "PROJECT#"),
            "recordingId": _attr(new, "recordingId"),
            "retryCount": int(_attr(new, "retryCount", "N")),
        }
        return "RetryRequested", {**_common(record), **detail}

    return None


def handler(event, context):
    with logs.scope(requestId=getattr(context, "aws_request_id", None)):
        return route(event)


def route(event):
    entries = []
    for record in event["Records"]:
        routed = to_event(record)
        if routed is None:
            logs.info("ignored", eventID=record["eventID"], eventName=record["eventName"])
            continue
        detail_type, detail = routed
        entries.append(events.put_events_entry(detail_type, detail, BUS_NAME))
        # A retried job is Queued again: the stage its trace continues from.
        logs.info("routed", detailType=detail_type, eventId=detail["eventId"], userId=detail["userId"],
                  jobId=detail.get("jobId"), projectId=detail.get("projectId"),
                  stage="Queued" if detail_type == "RetryRequested" else None)

    for start in range(0, len(entries), PUT_EVENTS_MAX_ENTRIES):
        batch = entries[start : start + PUT_EVENTS_MAX_ENTRIES]
        response = _events_client().put_events(Entries=batch)
        if response["FailedEntryCount"]:
            # PutEvents doesn't raise on partial failure. Raising retries the batch (bisected); the
            # entries that did go through are sent again with the same eventId.
            failed = [e for e in response["Entries"] if "ErrorCode" in e]
            raise RuntimeError(f"PutEvents failed for {len(failed)} of {len(batch)} entries: {failed}")

    return {"routed": len(entries)}
