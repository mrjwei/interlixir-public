"""Read-only DynamoDB and S3 access for the tools. Every function takes the verified userId and checks
ownership the way the API does (services/api/app/store.py): projects by key (USER#<userId>), jobs and documents
by their userId attribute, a project's contents only after the project lookup. A miss and someone else's item
both raise NotFound, so an ID can't be probed.

Nothing here writes: the server has no write path at all, so a prompt-injected tool call can only read the
caller's own data.
"""

import functools
import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "interlixir-docs")

# A tool call is one step of a chat turn, so it must fail fast: 3 attempts of at most 2 s connect + 5 s read.
TIMEOUTS = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "max_attempts": 3})

PROJECT_PREFIX = "PROJECT#"
DOC_PREFIX = "DOC#"
REC_PREFIX = "REC#"
EVENT_PREFIX = "EVENT#"
JOB_SK = "META"


class NotFound(Exception):
    """Missing, or owned by another user."""


@functools.cache
def table():
    return boto3.resource("dynamodb", region_name=REGION, config=TIMEOUTS).Table(TABLE_NAME)


@functools.cache
def s3():
    return boto3.client("s3", region_name=REGION, config=TIMEOUTS)


def _query_all(index=None, **kwargs):
    if index:
        kwargs["IndexName"] = index
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def list_projects(user_id):
    return _query_all(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with(PROJECT_PREFIX)
    )


def get_project(user_id, project_id):
    item = table().get_item(Key={"PK": f"USER#{user_id}", "SK": f"{PROJECT_PREFIX}{project_id}"}).get("Item")
    if item is None:
        raise NotFound
    return item


def project_contents(user_id, project_id):
    """(project, recordings, documents). Contents don't carry userId, so the project lookup comes first."""
    project = get_project(user_id, project_id)
    recordings, documents = [], []
    for item in _query_all(KeyConditionExpression=Key("PK").eq(f"{PROJECT_PREFIX}{project_id}")):
        if item["SK"].startswith(REC_PREFIX):
            recordings.append(item)
        elif item["SK"].startswith(DOC_PREFIX):
            documents.append(item)
    return project, recordings, documents


def get_document(user_id, project_id, document_id):
    item = table().get_item(Key={"PK": f"{PROJECT_PREFIX}{project_id}", "SK": f"{DOC_PREFIX}{document_id}"}).get("Item")
    # userId is written by synthesis; it is the ownership check, as in the API.
    if item is None or item.get("userId") != user_id:
        raise NotFound
    return item


def read_markdown(user_id, project_id, document_id):
    # Built from the caller's userId, never from the item's storageKey (same rule as the API).
    key = f"documents/{user_id}/{project_id}/{document_id}.md"
    return s3().get_object(Bucket=DOCS_BUCKET, Key=key)["Body"].read().decode("utf-8")


def get_job(user_id, job_id):
    item = table().get_item(Key={"PK": f"JOB#{job_id}", "SK": JOB_SK}).get("Item")
    if item is None or item.get("userId") != user_id:
        raise NotFound
    return item


def project_jobs(user_id, project_id, limit):
    """Up to limit of a project's Job items, through GSI1 (KEYS_ONLY, so each job is then read by key). Call it
    only after the project lookup; get_job checks the owner again."""
    keys = _query_all("GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"{PROJECT_PREFIX}{project_id}"))
    jobs = []
    for key in keys[:limit]:
        try:
            jobs.append(get_job(user_id, key["PK"].removeprefix("JOB#")))
        except NotFound:
            continue
    return jobs


def history(user_id, limit):
    page = table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with(EVENT_PREFIX),
        ScanIndexForward=False,
        Limit=limit,
    )
    return page["Items"]
