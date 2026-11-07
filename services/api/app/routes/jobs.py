"""Jobs: status (polled about every 3 s by the SPA, cached for 5 s) and retry."""

from fastapi import APIRouter, Response

from .. import cache, keys, store
from ..auth import UserId
from . import Id, NoCache

router = APIRouter(prefix="/a3/jobs", tags=["jobs"])


def _job(item):
    chunks_total = item.get("chunksTotal")
    token_count = item.get("tokenCount")
    cost = item.get("cost")
    audio_seconds = item.get("audioSeconds")
    return {
        "jobId": keys.strip(item["PK"], "JOB#"),
        "projectId": keys.strip(item["GSI1PK"], keys.PROJECT_PREFIX) if "GSI1PK" in item else None,
        "recordingId": item.get("recordingId"),
        "schemaId": item.get("schemaId"),
        "instructionText": item.get("instructionText"),
        "createdAt": item.get("createdAt"),
        "stage": item.get("stage"),
        "audioSeconds": float(audio_seconds) if audio_seconds is not None else None,
        "chunksTotal": int(chunks_total) if chunks_total is not None else None,
        # doneChunks is a set of chunk numbers; its size is the UI's "2/3".
        "chunksDone": len(item.get("doneChunks", ())),
        "retryCount": int(item.get("retryCount", 0)),
        "failureReason": item.get("failureReason"),
        # Written by synthesis with stage Done: the SPA opens the document from the job it polls.
        "documentId": item.get("documentId"),
        "tokenCount": int(token_count) if token_count is not None else None,
        "cost": float(cost) if cost is not None else None,
    }


@router.get("/{job_id}")
def get_job(job_id: Id, user_id: UserId, response: Response, no_cache: NoCache):
    return cache.aside(
        response, "job", cache.job_key(user_id, job_id), cache.JOB_TTL, lambda: _job(store.get_job(user_id, job_id)),
        no_cache,
    )


@router.post("/{job_id}/retry", status_code=202)
def retry_job(job_id: Id, user_id: UserId):
    """202: the job is Queued again; the pipeline picks it up asynchronously (RetryRequested)."""
    return _job(store.retry_job(user_id, job_id))
