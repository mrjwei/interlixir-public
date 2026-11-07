"""Single-table keys (the deployment notes, data-model table) and the ID format.

Every key the API reads or writes is built here. Owner-scoped keys take the caller's userId, which comes
only from the verified token (auth.py), never from the request.
"""

import datetime
import uuid

# Same as the event contract's id: no '#' or '/', which delimit DynamoDB and S3 keys. Path parameters are
# checked against it, so a crafted ID can't reach into another key range.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"

JOB_SK = "META"
BUILTIN_SCHEMA_PK = "SCHEMA#BUILTIN"
SCHEMA_PREFIX = "SCHEMA#"
PROJECT_PREFIX = "PROJECT#"
REC_PREFIX = "REC#"
DOC_PREFIX = "DOC#"
CTX_PREFIX = "CTX#"
EVENT_PREFIX = "EVENT#"


def new_id():
    """Time-ordered, so a project list sorted by SK is in creation order."""
    return str(uuid.uuid7())


def now():
    """UTC, milliseconds, fixed width: the event contract's occurredAt format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def strip(value, prefix):
    if not value.startswith(prefix):
        raise ValueError(f"expected {prefix}... got {value!r}")
    return value[len(prefix) :]


def user_pk(user_id):
    return f"USER#{user_id}"


def project_pk(project_id):
    return f"{PROJECT_PREFIX}{project_id}"


def project_sk(project_id):
    return f"{PROJECT_PREFIX}{project_id}"


def doc_sk(document_id):
    return f"{DOC_PREFIX}{document_id}"


def rec_sk(recording_id):
    return f"{REC_PREFIX}{recording_id}"


def ctx_sk(context_file_id):
    return f"{CTX_PREFIX}{context_file_id}"


def job_pk(job_id):
    return f"JOB#{job_id}"


def recording_object_key(user_id, project_id, recording_id, ext):
    """uploads bucket key. The job creator parses projectId and recordingId back out of it, so its shape
    is part of the contract with the job creator."""
    return f"recordings/{user_id}/{project_id}/{recording_id}.{ext}"


def context_object_key(user_id, project_id, context_file_id, ext):
    return f"context/{user_id}/{project_id}/{context_file_id}.{ext}"


def document_object_key(user_id, project_id, document_id):
    """docs bucket key. Built from the caller's userId rather than read from the item's storageKey, so a
    wrong attribute can't point a download at someone else's object."""
    return f"documents/{user_id}/{project_id}/{document_id}.md"
