"""DynamoDB and S3 access for the API. The service talks to nothing else: no SQS, EventBridge
or Bedrock. Side effects (retry, delete) are table writes that the stream router turns into events.

Ownership is checked here, not in the routes: a lookup that misses or belongs to someone else raises
NotFound, which the app maps to 404 (never 403, so IDs can't be probed).

Cache: the routes cache the responses they build (cache.aside); every write here deletes the keys
it affects once it has committed (cache.invalidate), so the caller's next read is a MISS.
"""

import functools
import os

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import ClientError
from contracts import logs

from . import cache, keys

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "interlixir-docs")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "interlixir-uploads")


class NotFound(Exception):
    """Missing, or owned by another user. The two must look the same to the caller."""


class Conflict(Exception):
    """The item exists and is the caller's, but isn't in a state that allows the change."""


# Bounded: API Gateway gives up on the integration after 30 s, so a request's calls must fail well before
# that. 3 attempts of at most 2 s connect + 5 s read, with the standard mode's jittered exponential backoff
# (it also retries throttling). DynamoDB and S3 normally answer in milliseconds.
TIMEOUTS = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "max_attempts": 3})


@functools.cache
def table():
    return boto3.resource("dynamodb", region_name=REGION, config=TIMEOUTS).Table(TABLE_NAME)


@functools.cache
def s3():
    # Virtual-hosted, regional URLs (https://<bucket>.s3.ap-southeast-2.amazonaws.com/). The default is the
    # global host, which can answer a browser's POST with a redirect the upload doesn't survive.
    return boto3.client(
        "s3", region_name=REGION,
        config=TIMEOUTS.merge(Config(signature_version="s3v4", s3={"addressing_style": "virtual"})),
    )


def _query_all(**kwargs):
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- Projects ----------------------------------------------------------------------------------------


def list_projects(user_id):
    return _query_all(
        KeyConditionExpression=Key("PK").eq(keys.user_pk(user_id)) & Key("SK").begins_with(keys.PROJECT_PREFIX)
    )


def create_project(user_id, name):
    project_id = keys.new_id()
    item = {
        "PK": keys.user_pk(user_id),
        "SK": keys.project_sk(project_id),
        "projectName": name,
        "createdAt": keys.now(),
    }
    table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    cache.invalidate(cache.projects_key(user_id))
    return item


def get_project(user_id, project_id):
    item = table().get_item(Key={"PK": keys.user_pk(user_id), "SK": keys.project_sk(project_id)}).get("Item")
    if item is None:
        raise NotFound
    return item


def rename_project(user_id, project_id, name):
    """A Project MODIFY: the stream router's filters drop it, so nothing else happens."""
    client = table().meta.client
    try:
        item = table().update_item(
            Key={"PK": keys.user_pk(user_id), "SK": keys.project_sk(project_id)},
            UpdateExpression="SET projectName = :name",
            ConditionExpression="attribute_exists(PK)",  # an update would otherwise create the project
            ExpressionAttributeValues={":name": name},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except client.exceptions.ConditionalCheckFailedException:
        raise NotFound from None
    cache.invalidate(cache.projects_key(user_id), cache.project_key(user_id, project_id))
    return item


def delete_project(user_id, project_id):
    """Delete the Project item only. The sidebar drops it at once; the stream router turns the REMOVE into
    DeleteRequested, and GC removes the project's recordings, jobs, documents and files."""
    client = table().meta.client
    try:
        table().delete_item(
            Key={"PK": keys.user_pk(user_id), "SK": keys.project_sk(project_id)},
            ConditionExpression="attribute_exists(PK)",
        )
    except client.exceptions.ConditionalCheckFailedException:
        raise NotFound from None
    cache.invalidate(cache.projects_key(user_id), cache.project_key(user_id, project_id))


def list_project_contents(user_id, project_id):
    """Recordings, documents and context files. Those items don't carry userId, so the ownership check is
    the project lookup first."""
    get_project(user_id, project_id)
    return _query_all(KeyConditionExpression=Key("PK").eq(keys.project_pk(project_id)))


# --- Uploads -----------------------------------------------------------------------------------


def create_uploads(user_id, project_id, schema_id, instruction_text, recordings, context_files):
    """Pre-create everything one upload needs, in one transaction: per recording a Recording item (with
    its jobId) and a Queued Job; per context file a ContextFile item; and a UsesAsContext link from every
    job to every context file. recordings and context_files are lists of (fileName, ext).

    The transaction also checks the project still exists, so a project deleted mid-request leaves nothing
    behind. Returns (recordings, context_files) as lists of dicts with the new IDs and object keys.
    """
    created_at = keys.now()
    contexts = []
    for file_name, ext in context_files:
        context_file_id = keys.new_id()
        contexts.append(
            {
                "contextFileId": context_file_id,
                "fileName": file_name,
                "storageKey": keys.context_object_key(user_id, project_id, context_file_id, ext),
            }
        )
    recs = []
    for file_name, ext in recordings:
        recording_id = keys.new_id()
        recs.append(
            {
                "recordingId": recording_id,
                "jobId": keys.new_id(),
                "fileName": file_name,
                "storageKey": keys.recording_object_key(user_id, project_id, recording_id, ext),
            }
        )

    def put(item):
        return {"Put": {"TableName": TABLE_NAME, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}}

    writes = [
        {
            "ConditionCheck": {
                "TableName": TABLE_NAME,
                "Key": {"PK": keys.user_pk(user_id), "SK": keys.project_sk(project_id)},
                "ConditionExpression": "attribute_exists(PK)",
            }
        }
    ]
    for ctx in contexts:
        writes.append(
            put(
                {
                    "PK": keys.project_pk(project_id),
                    "SK": keys.ctx_sk(ctx["contextFileId"]),
                    "fileName": ctx["fileName"],
                    "storageKey": ctx["storageKey"],
                    "createdAt": created_at,
                }
            )
        )
    for rec in recs:
        writes.append(
            put(
                {
                    "PK": keys.project_pk(project_id),
                    "SK": keys.rec_sk(rec["recordingId"]),
                    "fileName": rec["fileName"],
                    "storageKey": rec["storageKey"],
                    "languageCode": "en",
                    "jobId": rec["jobId"],
                    "createdAt": created_at,
                }
            )
        )
        job = {
            "PK": keys.job_pk(rec["jobId"]),
            "SK": keys.JOB_SK,
            "GSI1PK": keys.project_pk(project_id),
            "GSI1SK": keys.job_pk(rec["jobId"]),
            "userId": user_id,
            "recordingId": rec["recordingId"],
            "schemaId": schema_id,
            "stage": "Queued",
            "retryCount": 0,
            "createdAt": created_at,
        }
        if instruction_text:
            job["instructionText"] = instruction_text
        writes.append(put(job))
        for ctx in contexts:
            writes.append(put({"PK": keys.job_pk(rec["jobId"]), "SK": keys.ctx_sk(ctx["contextFileId"])}))

    client = table().meta.client  # a resource's client takes plain Python values, like the Table does
    try:
        client.transact_write_items(TransactItems=writes)
    except client.exceptions.TransactionCanceledException as error:
        # Reasons are in TransactItems order; the first is the project check.
        reasons = error.response.get("CancellationReasons", [])
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            raise NotFound from None
        raise
    cache.invalidate(cache.project_key(user_id, project_id))  # the new recordings
    return recs, contexts


def presign_upload(key, content_type, max_bytes, expires_in):
    """A pre-signed POST policy for one object: exact key, exact Content-Type, 1..max_bytes. S3 checks
    every condition before it stores anything. {"url", "fields"}; the client posts the fields, then the file
    last. Valid for expires_in or until the task role's credentials expire, whichever is first."""
    return s3().generate_presigned_post(
        UPLOADS_BUCKET,
        key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=expires_in,
    )


# --- Documents ---------------------------------------------------------------------------------------


def get_document(user_id, project_id, document_id):
    item = table().get_item(Key={"PK": keys.project_pk(project_id), "SK": keys.doc_sk(document_id)}).get("Item")
    # userId is written by synthesis for the stream router; it doubles as the ownership check here.
    if item is None or item.get("userId") != user_id:
        raise NotFound
    return item


def read_document(user_id, project_id, document_id):
    key = keys.document_object_key(user_id, project_id, document_id)
    return s3().get_object(Bucket=DOCS_BUCKET, Key=key)["Body"].read().decode("utf-8")


def update_document(user_id, project_id, document_id, title=None, markdown=None):
    """Rename and/or replace the Markdown. Versioning is off, so an edit overwrites the object. A rename on
    its own also rewrites the Markdown's first line if it is still the generated `# <old title>`, so an export
    carries the new name. Document MODIFY is dropped by the stream router's filters. Returns the updated item."""
    item = get_document(user_id, project_id, document_id)
    if markdown is not None:
        _put_markdown(user_id, project_id, document_id, markdown)
    elif title is not None:
        current = read_document(user_id, project_id, document_id)
        first, newline, rest = current.partition("\n")
        if first == f"# {item.get('documentTitle')}":
            try:
                _put_markdown(user_id, project_id, document_id, f"# {title}{newline}{rest}")
            except ClientError as error:  # the rename is what was asked for; the heading is a courtesy
                logs.warning("rename: heading not rewritten", userId=user_id, projectId=project_id,
                             documentId=document_id, error=repr(error))
    values = {":userId": user_id, ":now": keys.now()}
    update = "SET updatedAt = :now"
    if title is not None:
        update += ", documentTitle = :title"
        values[":title"] = title
    client = table().meta.client
    try:
        item = table().update_item(
            Key={"PK": keys.project_pk(project_id), "SK": keys.doc_sk(document_id)},
            UpdateExpression=update,
            # Also fails if the document was deleted meanwhile, so the update can't recreate it.
            ConditionExpression="userId = :userId",
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except client.exceptions.ConditionalCheckFailedException:
        raise NotFound from None
    if title is not None:
        cache.invalidate(cache.project_key(user_id, project_id))  # the project's contents list document titles
    return item


def _put_markdown(user_id, project_id, document_id, markdown):
    s3().put_object(
        Bucket=DOCS_BUCKET,
        Key=keys.document_object_key(user_id, project_id, document_id),
        Body=markdown.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


def delete_document(user_id, project_id, document_id):
    """Delete the Document and the Recording it was made from, in one transaction, so the sidebar, which
    lists recordings, drops the entry at once. The stream router turns the Document's REMOVE (which names the job and
    recording) into DeleteRequested; GC removes the job, the audio, chunks, transcripts, the .md and any context file
    no other job uses."""
    item = get_document(user_id, project_id, document_id)
    job_id = item.get("jobId")
    recording_id = item.get("recordingId")
    if recording_id is None and job_id:  # Documents from before synthesis wrote recordingId
        recording_id = get_job(user_id, job_id).get("recordingId")
    writes = [{
        "Delete": {
            "TableName": TABLE_NAME,
            "Key": {"PK": keys.project_pk(project_id), "SK": keys.doc_sk(document_id)},
            "ConditionExpression": "userId = :userId",
            "ExpressionAttributeValues": {":userId": user_id},
        }
    }]
    if recording_id:
        writes.append({"Delete": {"TableName": TABLE_NAME,
                                  "Key": {"PK": keys.project_pk(project_id), "SK": keys.rec_sk(recording_id)}}})
    client = table().meta.client
    try:
        client.transact_write_items(TransactItems=writes)
    except client.exceptions.TransactionCanceledException as error:
        reasons = error.response.get("CancellationReasons", [])
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            raise NotFound from None  # deleted by a concurrent request
        raise
    stale = [cache.project_key(user_id, project_id)]
    if job_id:
        stale.append(cache.job_key(user_id, job_id))
    cache.invalidate(*stale)


def job_context_files(project_id, job_id):
    """The ContextFile items a job used: its UsesAsContext links (JOB#<jobId> / CTX#<id>), resolved against
    the project's ContextFile items. Call it only after an ownership check on the project or the job."""
    if not job_id:
        return []
    linked = {
        item["SK"]
        for item in _query_all(
            KeyConditionExpression=Key("PK").eq(keys.job_pk(job_id)) & Key("SK").begins_with(keys.CTX_PREFIX)
        )
    }
    if not linked:
        return []
    files = _query_all(
        KeyConditionExpression=Key("PK").eq(keys.project_pk(project_id)) & Key("SK").begins_with(keys.CTX_PREFIX)
    )
    return [f for f in files if f["SK"] in linked]


def presign_document_download(user_id, project_id, document_id, filename, expires_in):
    """Pre-signed GET that downloads as a file. It's valid for expires_in or until the task role's
    credentials expire, whichever is first."""
    return s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": DOCS_BUCKET,
            "Key": keys.document_object_key(user_id, project_id, document_id),
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
            "ResponseContentType": "text/markdown; charset=utf-8",
        },
        ExpiresIn=expires_in,
    )


# --- Jobs --------------------------------------------------------------------------------------------


def get_job(user_id, job_id):
    item = table().get_item(Key={"PK": keys.job_pk(job_id), "SK": keys.JOB_SK}).get("Item")
    if item is None or item.get("userId") != user_id:
        raise NotFound
    return item


def retry_job(user_id, job_id):
    """Failed -> Queued, retryCount + 1, in one conditional update. The stream router sees the
    change and emits RetryRequested; nothing else happens here."""
    client = table().meta.client
    try:
        item = table().update_item(
            Key={"PK": keys.job_pk(job_id), "SK": keys.JOB_SK},
            UpdateExpression="SET #stage = :queued ADD retryCount :one",
            ConditionExpression="userId = :userId AND #stage = :failed",
            ExpressionAttributeNames={"#stage": "stage"},
            ExpressionAttributeValues={":queued": "Queued", ":failed": "Failed", ":userId": user_id, ":one": 1},
            ReturnValues="ALL_NEW",
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )["Attributes"]
    except client.exceptions.ConditionalCheckFailedException as error:
        # The resource API doesn't deserialise the item that comes back with the error.
        old = {k: TypeDeserializer().deserialize(v) for k, v in error.response.get("Item", {}).items()}
        if old.get("userId") != user_id:
            raise NotFound from None
        raise Conflict(f"Job is {old.get('stage')}; only a Failed job can be retried") from None
    cache.invalidate(cache.job_key(user_id, job_id))  # the next poll must show Queued, not the cached Failed
    return item


# --- Schemas -----------------------------------------------------------------------------------------


def list_schemas(user_id):
    """(built-in, the user's own). Built-ins live under SCHEMA#BUILTIN; a user's own under USER#<userId>."""
    fields = {
        "ProjectionExpression": "SK, #name, description",
        "ExpressionAttributeNames": {"#name": "name"},
    }
    builtin = _query_all(KeyConditionExpression=Key("PK").eq(keys.BUILTIN_SCHEMA_PK), **fields)
    own = _query_all(
        KeyConditionExpression=Key("PK").eq(keys.user_pk(user_id)) & Key("SK").begins_with(keys.SCHEMA_PREFIX),
        **fields,
    )
    return builtin, own


def schema_exists(user_id, schema_id):
    """A job may only use a built-in schema or one of the caller's own (data-model rule)."""
    for pk in (keys.BUILTIN_SCHEMA_PK, keys.user_pk(user_id)):
        key = {"PK": pk, "SK": f"{keys.SCHEMA_PREFIX}{schema_id}"}
        if "Item" in table().get_item(Key=key, ProjectionExpression="PK"):
            return True
    return False


# --- History -----------------------------------------------------------------------------------------


def history_page(user_id, limit, start_key=None):
    """One page of the user's events, newest first: (items, LastEvaluatedKey or None)."""
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(keys.user_pk(user_id)) & Key("SK").begins_with(keys.EVENT_PREFIX),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if start_key is not None:
        kwargs["ExclusiveStartKey"] = start_key
    page = table().query(**kwargs)
    return page["Items"], page.get("LastEvaluatedKey")
