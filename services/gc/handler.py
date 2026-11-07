"""Garbage collector: DeleteRequested -> remove everything that belonged to the deleted Project or Document.

Trigger: the API deletes a Project item (or a Document and its Recording, in one transaction), the stream router
turns the REMOVE into DeleteRequested on the bus, rule interlixir-delete-requested -> SQS interlixir-gc -> this
function (batch size 1, ReportBatchItemFailures). Each SQS body is the EventBridge event. A direct rule -> Lambda
target works too: the function then gets the event itself.

Nothing in S3 expires (lifecycle rules are denied, the account's permission limits), so this is the only thing that removes a
project's raw audio, chunks, transcripts, context files and documents.

  Project   every job of the project (GSI1): chunks/<jobId>/, transcripts/<userId>/<jobId>/, its UsesAsContext links,
            then the Job item; recordings/<userId>/<projectId>/ and context/<userId>/<projectId>/; then every item
            in PROJECT#<projectId> (a Document's .md first). Removing the Documents emits more DeleteRequested
            events, which find nothing left to do.
  Document  the .md; the recording's object and Recording item; the job's context files that no other job of the
            project uses; the job's chunks, transcripts and links, then the Job item.

Idempotent and safe to re-run: every step deletes what is there and skips what isn't, and the Job item, which is how a
re-run finds the rest, goes last. A DeleteRequested for a Project or Document that exists (a stale or replayed event)
is skipped. Ownership is checked against the event's userId before anything is removed: a Job by its userId, a
Document by its userId, a Recording or ContextFile by the user prefix of its storageKey.

Known gap: a worker or synthesis run already holding a deleted job can still write one transcript or step file
after this has run. Those writers stop at the job item (it's gone), so at most that file is left behind.
"""

import json
import logging
import os
import re
import time
import traceback
from decimal import Decimal

from contracts import events, logs

logs.configure("gc")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "interlixir-uploads")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "interlixir-docs")

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
JOB_SK = "META"
DELETE_OBJECTS_MAX = 1000

_clients = {}


def _client(name):
    if name not in _clients:
        import boto3  # in the Lambda runtime; imported here so the tests can patch the clients
        from botocore.config import Config

        # Bounded: 3 attempts of at most 3 + 10 s each, well inside the function's 120 s timeout.
        config = Config(connect_timeout=3, read_timeout=10, retries={"mode": "standard", "max_attempts": 3})
        if name == "table":
            _clients[name] = boto3.resource("dynamodb", region_name=REGION, config=config).Table(TABLE_NAME)
        else:
            _clients[name] = boto3.client(name, region_name=REGION, config=config)
    return _clients[name]


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
                logs.error("delete not finished, will be redelivered", **fields, error=repr(error),
                           trace=traceback.format_exc())
                failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handle_event(event, fields, raw=None):
    detail = delete_request(event)
    if detail is None:
        _log("dropped: not a DeleteRequested event", logging.WARNING, **fields,
             body=(raw if raw is not None else json.dumps(event, default=str))[:4000])
        return
    fields = {**fields, "eventId": detail["eventId"], "entity": detail["entity"], "userId": detail["userId"],
              "projectId": detail["projectId"]}
    if detail["entity"] == "Document":
        fields["documentId"] = detail["documentId"]
    started = time.monotonic()
    counts = Counts()
    if detail["entity"] == "Project":
        if _get(f"USER#{detail['userId']}", f"PROJECT#{detail['projectId']}") is not None:
            _log("skipped: project still exists", **fields)
            return
        collect_project(detail["userId"], detail["projectId"], counts)
    else:
        if _get(f"PROJECT#{detail['projectId']}", f"DOC#{detail['documentId']}") is not None:
            _log("skipped: document still exists", **fields)
            return
        collect_document(detail, counts)
    # objects: those found by prefix (documents are deleted by key, uncounted); items: those that were still there.
    _log("collected", **fields, objects=counts.objects, items=counts.items, skipped=counts.skipped,
         durationMs=logs.ms_since(started))


def delete_request(event):
    """The detail of a valid DeleteRequested event, or None. Checks what the keys and prefixes are built from: an ID
    with a '/' or '#' would reach into another user's prefix or key range."""
    if not isinstance(event, dict) or event.get("source") != events.SOURCE:
        return None
    detail = event.get("detail")
    if event.get("detail-type") != "DeleteRequested" or not isinstance(detail, dict):
        return None
    if not _ids(detail, "eventId", "userId", "projectId"):
        return None
    if detail.get("entity") == "Project":
        return None if any(n in detail for n in ("documentId", "jobId", "recordingId")) else detail
    if detail.get("entity") != "Document" or not _ids(detail, "documentId"):
        return None
    if any(n in detail and not _ids(detail, n) for n in ("jobId", "recordingId")):
        return None
    return detail


def _ids(detail, *names):
    return all(isinstance(detail.get(n), str) and ID_PATTERN.fullmatch(detail[n]) for n in names)


class Counts:
    def __init__(self):
        self.objects = self.items = self.skipped = 0


# --- Project -----------------------------------------------------------------------------------------


def collect_project(user_id, project_id, counts):
    for job_id in project_job_ids(project_id):
        job = _get(f"JOB#{job_id}", JOB_SK)
        if job is not None and job.get("userId") != user_id:
            _log("skipped: job of another user", logging.WARNING, jobId=job_id, projectId=project_id)
            counts.skipped += 1
            continue
        remove_job(user_id, job_id, counts)
    counts.objects += delete_prefix(UPLOADS_BUCKET, f"recordings/{user_id}/{project_id}/")
    counts.objects += delete_prefix(UPLOADS_BUCKET, f"context/{user_id}/{project_id}/")
    for item in _query_all(f"PROJECT#{project_id}"):
        if not owned(item, user_id):
            _log("skipped: item of another user", logging.WARNING, PK=item["PK"], SK=item["SK"])
            counts.skipped += 1
            continue
        if item["SK"].startswith("DOC#"):
            delete_key(DOCS_BUCKET, document_key(user_id, project_id, item["SK"][len("DOC#"):]))
        _delete(item["PK"], item["SK"], counts)


def project_job_ids(project_id):
    """Job IDs from GSI1 (KEYS_ONLY: only Job items carry its keys). Eventually consistent, but the API stops new
    jobs at the project check in the upload transaction, so only a job created in the same second could be missed."""
    job_ids, kwargs = [], {"IndexName": "GSI1", "KeyConditionExpression": "GSI1PK = :pk",
                           "ExpressionAttributeValues": {":pk": f"PROJECT#{project_id}"}}
    while True:
        page = _client("table").query(**kwargs)
        job_ids += [item["PK"][len("JOB#"):] for item in page["Items"] if item["PK"].startswith("JOB#")]
        if "LastEvaluatedKey" not in page:
            return job_ids
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- Document ----------------------------------------------------------------------------------------


def collect_document(detail, counts):
    user_id, project_id, document_id = detail["userId"], detail["projectId"], detail["documentId"]
    job_id = detail.get("jobId", document_id)  # synthesis uses the job's ID as its document's (one job, one document)
    job = _get(f"JOB#{job_id}", JOB_SK)
    if job is not None and (job.get("userId") != user_id or job.get("GSI1PK") != f"PROJECT#{project_id}"):
        _log("skipped: job of another user or project", logging.WARNING, jobId=job_id)
        counts.skipped += 1
        job_id, job = None, None
    recording_id = detail.get("recordingId") or (job or {}).get("recordingId")

    delete_key(DOCS_BUCKET, document_key(user_id, project_id, document_id))
    if recording_id and ID_PATTERN.fullmatch(recording_id):
        # The extension isn't in the event, so the recording is found by prefix (<recordingId>.<ext>).
        counts.objects += delete_prefix(UPLOADS_BUCKET, f"recordings/{user_id}/{project_id}/{recording_id}.")
        rec = _get(f"PROJECT#{project_id}", f"REC#{recording_id}")
        if rec is not None:  # the API deletes it with the Document; this covers a Document removed some other way
            if owned(rec, user_id):
                _delete(rec["PK"], rec["SK"], counts)
            else:
                counts.skipped += 1
    if job_id is not None:
        remove_unshared_context(user_id, project_id, job_id, counts)
        remove_job(user_id, job_id, counts)


def remove_unshared_context(user_id, project_id, job_id, counts):
    """Context files are uploaded once for every recording in the same upload (one UsesAsContext link per job), so
    a file goes only when no other job of the project still links to it. The links are read before remove_job
    deletes them, so a re-run after a crash still sees them."""
    context_ids = [item["SK"][len("CTX#"):] for item in _query_all(f"JOB#{job_id}", "CTX#")]
    if not context_ids:
        return
    others = [j for j in project_job_ids(project_id) if j != job_id]
    for context_id in context_ids:
        if any(_get(f"JOB#{other}", f"CTX#{context_id}") is not None for other in others):
            continue
        counts.objects += delete_prefix(UPLOADS_BUCKET, f"context/{user_id}/{project_id}/{context_id}.")  # file + .txt
        ctx = _get(f"PROJECT#{project_id}", f"CTX#{context_id}")
        if ctx is not None and owned(ctx, user_id):
            _delete(ctx["PK"], ctx["SK"], counts)


# --- shared ------------------------------------------------------------------------------------------


def remove_job(user_id, job_id, counts):
    """A job's pipeline files and its items. The Job item goes last: it's how a re-run finds the job."""
    counts.objects += delete_prefix(UPLOADS_BUCKET, f"chunks/{job_id}/")
    counts.objects += delete_prefix(UPLOADS_BUCKET, f"transcripts/{user_id}/{job_id}/")
    for link in _query_all(f"JOB#{job_id}", "CTX#"):
        _delete(link["PK"], link["SK"], counts)
    _delete(f"JOB#{job_id}", JOB_SK, counts)


def owned(item, user_id):
    """Whether an item in a project's partition is this user's. Documents carry userId (synthesis); Recordings and
    ContextFiles a storageKey the API built from the verified userId."""
    sk = item["SK"]
    if sk.startswith("DOC#"):
        return item.get("userId") == user_id
    if sk.startswith("REC#"):
        return str(item.get("storageKey", "")).startswith(f"recordings/{user_id}/")
    if sk.startswith("CTX#"):
        return str(item.get("storageKey", "")).startswith(f"context/{user_id}/")
    return False


def document_key(user_id, project_id, document_id):
    return f"documents/{user_id}/{project_id}/{document_id}.md"


# --- AWS calls ---------------------------------------------------------------------------------------


def delete_prefix(bucket, prefix):
    """Delete every object under prefix; the number deleted. Listing needs s3:ListBucket, which the role has on the
    uploads bucket, so the docs bucket is only ever deleted from by exact key."""
    s3, deleted, kwargs = _client("s3"), 0, {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for start in range(0, len(keys), DELETE_OBJECTS_MAX):
            batch = keys[start : start + DELETE_OBJECTS_MAX]
            response = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            if response.get("Errors"):  # DeleteObjects answers 200 even when some keys failed
                raise RuntimeError(f"DeleteObjects failed for {len(response['Errors'])} keys: {response['Errors'][:3]}")
            deleted += len(batch)
        if not page.get("IsTruncated"):
            return deleted
        kwargs["ContinuationToken"] = page["NextContinuationToken"]


def delete_key(bucket, key):
    """Delete one object by its key. S3 answers 204 whether or not it was there, so it isn't counted."""
    _client("s3").delete_object(Bucket=bucket, Key=key)


def _delete(pk, sk, counts):
    """Delete one item, counted only if it was there (a re-run or a cascade finds most of them gone)."""
    old = _client("table").delete_item(Key={"PK": pk, "SK": sk}, ReturnValues="ALL_OLD").get("Attributes")
    counts.items += old is not None


def _get(pk, sk):
    return _client("table").get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=True).get("Item")


def _query_all(pk, sk_prefix=None):
    from boto3.dynamodb.conditions import Key

    condition = Key("PK").eq(pk)
    if sk_prefix:
        condition &= Key("SK").begins_with(sk_prefix)
    items, kwargs = [], {"KeyConditionExpression": condition, "ConsistentRead": True}
    while True:
        page = _client("table").query(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
