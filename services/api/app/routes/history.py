"""History: the user's events, newest first, a page at a time, each page cached for 30 s by cursor.

The cursor is the page's LastEvaluatedKey, base64url-encoded. It's opaque to the client but not trusted:
a cursor whose PK isn't the caller's partition is rejected, so it can't page through someone else's events.
"""

import base64
import binascii
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from .. import cache, keys, store
from ..auth import UserId
from . import NoCache

router = APIRouter(prefix="/a3/history", tags=["history"])


def encode_cursor(last_key):
    raw = json.dumps(last_key, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor, user_id):
    """The ExclusiveStartKey for a cursor, or ValueError if it isn't one of this user's."""
    try:
        start_key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("malformed cursor") from None
    if (
        not isinstance(start_key, dict)
        or set(start_key) != {"PK", "SK"}
        or start_key["PK"] != keys.user_pk(user_id)
        or not isinstance(start_key["SK"], str)
        or not start_key["SK"].startswith(keys.EVENT_PREFIX)
    ):
        raise ValueError("cursor is not for this user's history")
    return start_key


def _event(item):
    # SK = EVENT#<occurredAt>#<eventId>; neither part can contain '#'.
    occurred_at, event_id = keys.strip(item["SK"], keys.EVENT_PREFIX).split("#", 1)
    event = {"eventId": event_id, "eventType": item.get("eventType"), "occurredAt": occurred_at}
    # projectId and fileName are copied from the job by the history writer; stage is where a JobFailed failed.
    for name in ("jobId", "chatSessionId", "projectId", "fileName", "stage"):
        if name in item:
            event[name] = item[name]
    if "tokenCount" in item:
        event["tokenCount"] = int(item["tokenCount"])
    if "cost" in item:
        event["cost"] = float(item["cost"])
    return event


@router.get("")
def list_history(
    user_id: UserId,
    response: Response,
    no_cache: NoCache,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
    cursor: str | None = None,
):
    start_key = None
    if cursor:
        try:
            start_key = decode_cursor(cursor, user_id)  # before the cache: a bad cursor is a 400, never cached
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    def load():
        items, last_key = store.history_page(user_id, limit, start_key)
        return {"events": [_event(i) for i in items], "nextCursor": encode_cursor(last_key) if last_key else None}

    return cache.aside(
        response, "history", cache.history_key(user_id, limit, cursor), cache.HISTORY_TTL, load, no_cache
    )
