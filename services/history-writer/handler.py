"""History writer: the five history events on the bus -> one Event item each in the user's
partition, USER#<userId> / EVENT#<occurredAt>#<eventId>, which GET /a3/history pages through newest first.

Trigger: rule interlixir-history (the five HISTORY_DETAIL_TYPES) -> SQS interlixir-history -> this function
(batch size 10, ReportBatchItemFailures). Each SQS body is the EventBridge event. A direct rule -> Lambda target
works too: the function then gets the event itself.

At least once, twice over: EventBridge and SQS can each deliver an event more than once, and producers re-emit an
event after a crash with the same eventId but a new occurredAt (they stamp it when they put it). So the Event
item's key can't be the dedupe key. Each event first claims its eventId:

  claim  USER#<userId> / EVENTID#<eventId>  SET eventSk = <the Event item's SK>
         condition: attribute_not_exists(PK) OR eventSk = <that SK>
  then   USER#<userId> / EVENT#<occurredAt>#<eventId>  SET eventType, jobId | chatSessionId, ...

A redelivery of the same emission carries the same occurredAt, so it passes its own claim and rewrites the same
item (a crash between the two writes is finished by the redelivery). A re-emission has a different occurredAt,
fails the condition and is dropped, so the user sees each event once, at its first time. Both are UpdateItem
calls: PutItem is unproven on lambda-role and an update creates the item.

Job events are denormalised with the job's projectId and its recording's fileName, read once here, so the history
panel can name and link the job without a read per row. A job that's gone (project deleted) is recorded without them.

An event that isn't a history event of this contract is logged in full and dropped (it would never succeed); an
AWS error fails the record, which SQS redelivers and, after 3 receives, moves to interlixir-history-dlq.

Lambdas can't reach the cache, so a history page cached by the API shows a new event once its 30 s TTL ends.
"""

import datetime
import json
import logging
import os
import re
import traceback
from decimal import Decimal

from contracts import events, logs

logs.configure("history-writer")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
OCCURRED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
FAILED_STAGES = ("Queued", "Transcribing", "Redacting", "Synthesising")
JOB_SK = "META"
# A producer re-emits within its queue's retention (14 days at most, the DLQs), so a claim can expire after that.
CLAIM_DAYS = 30

_clients = {}


def _table():
    if "table" not in _clients:
        import boto3  # in the Lambda runtime; imported here so the tests can patch the client
        from botocore.config import Config

        # Bounded: 3 attempts of at most 3 + 5 s each fit the function's 30 s timeout.
        config = Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard", "max_attempts": 3})
        _clients["table"] = boto3.resource("dynamodb", region_name=REGION, config=config).Table(TABLE_NAME)
    return _clients["table"]


def _log(msg, level=logging.INFO, **fields):
    (logs.warning if level >= logging.WARNING else logs.info)(msg, **fields)


# --- entry point -------------------------------------------------------------------------------------


def handler(event, context):
    request = {"requestId": getattr(context, "aws_request_id", None)}
    if "Records" not in event:  # a direct EventBridge target: an error makes Lambda retry the event
        with logs.scope(**request):
            handle_event(event, {})
        return None
    failures = []
    for record in event["Records"]:
        fields = {"messageId": record["messageId"]}
        try:
            body = json.loads(record["body"], parse_float=Decimal)
        except ValueError:
            body = None
        with logs.scope(**request):
            try:
                handle_event(body, fields, raw=record["body"])
            except Exception as error:
                logs.error("event not recorded, will be redelivered", **fields, error=repr(error),
                           trace=traceback.format_exc())
                failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handle_event(event, fields, raw=None):
    item = event_item(event)
    if item is None:
        # Logged whole, so a producer's contract bug can be read after the message is gone.
        _log("dropped: not a history event", logging.WARNING, **fields,
             body=(raw if raw is not None else json.dumps(event, default=str))[:4000])
        return
    fields = {**fields, "eventId": item["eventId"], "eventType": item["eventType"], "occurredAt": item["occurredAt"],
              "userId": item["userId"]}
    if "jobId" in item:
        fields["jobId"] = item["jobId"]
    if "stage" in item:
        fields["stage"] = item["stage"]
    if not claim(item):
        _log("skipped: duplicate eventId", **fields)
        return
    if "jobId" in item:
        item.update(job_context(item["jobId"], item["userId"]))
    write_event(item)
    _log("event recorded", **fields)


# --- the event ---------------------------------------------------------------------------------------


def event_item(event):
    """The Event item's attributes for a valid history event (plus eventId, userId, occurredAt for the keys),
    or None. Checks what the keys and the history API depend on; the full schema is the producers' tests' job."""
    if not isinstance(event, dict) or event.get("source") != events.SOURCE:
        return None
    event_type, detail = event.get("detail-type"), event.get("detail")
    if event_type not in events.HISTORY_DETAIL_TYPES or not isinstance(detail, dict):
        return None
    if not _ids(detail, "eventId", "userId") or not OCCURRED_AT.fullmatch(str(detail.get("occurredAt", ""))):
        return None
    item = {"eventType": event_type, "eventId": detail["eventId"], "userId": detail["userId"],
            "occurredAt": detail["occurredAt"]}
    owner = "chatSessionId" if event_type == "ChatSession" else "jobId"
    if not _ids(detail, owner):
        return None
    item[owner] = detail[owner]
    if event_type == "JobFailed":
        if detail.get("stage") not in FAILED_STAGES:
            return None
        item["stage"] = detail["stage"]
    for name in ("tokenCount", "cost"):
        value = detail.get(name)
        if value is None:
            if event_type in ("SynthesisDone", "ChatSession"):
                return None  # required for these two
            continue
        if isinstance(value, float):  # a direct invocation's event is parsed by Lambda, with floats
            value = Decimal(str(value))
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0:
            return None
        if name == "tokenCount" and not isinstance(value, int):
            return None
        item[name] = value
    return item


def _ids(detail, *names):
    return all(isinstance(detail.get(n), str) and ID_PATTERN.fullmatch(detail[n]) for n in names)


def job_context(job_id, user_id):
    """projectId and fileName for the history row, or {} if the job is gone or isn't this user's."""
    job = _get(f"JOB#{job_id}", JOB_SK)
    if job is None or job.get("userId") != user_id or not str(job.get("GSI1PK", "")).startswith("PROJECT#"):
        return {}
    project_id = job["GSI1PK"].removeprefix("PROJECT#")
    context = {"projectId": project_id}
    recording = _get(f"PROJECT#{project_id}", f"REC#{job.get('recordingId', '')}") if job.get("recordingId") else None
    if recording and recording.get("fileName"):
        context["fileName"] = recording["fileName"]
    return context


# --- DynamoDB ----------------------------------------------------------------------------------------


def _keys(item):
    return f"USER#{item['userId']}", f"EVENT#{item['occurredAt']}#{item['eventId']}"


def claim(item):
    """Claim the eventId for this emission. False if another emission of it (another occurredAt) got there first."""
    pk, sk = _keys(item)
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=CLAIM_DAYS)
    table = _table()
    try:
        table.update_item(
            Key={"PK": pk, "SK": f"EVENTID#{item['eventId']}"},
            UpdateExpression="SET eventSk = :sk, expiresAt = if_not_exists(expiresAt, :expires)",
            ConditionExpression="attribute_not_exists(PK) OR eventSk = :sk",
            ExpressionAttributeValues={":sk": sk, ":expires": int(expires.timestamp())},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def write_event(item):
    """The Event item. Unconditional: a redelivery writes the same values again."""
    pk, sk = _keys(item)
    values = {k: v for k, v in item.items() if k not in ("eventId", "userId", "occurredAt")}
    _table().update_item(
        Key={"PK": pk, "SK": sk},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in values),
        ExpressionAttributeNames={f"#{k}": k for k in values},
        ExpressionAttributeValues={f":{k}": v for k, v in values.items()},
    )


def _get(pk, sk):
    return _table().get_item(Key={"PK": pk, "SK": sk}).get("Item")
