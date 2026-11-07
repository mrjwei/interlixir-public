"""Chat sessions and messages in the single table (the deployment notes), and the ChatSession event.

  ChatSession  USER#<userId>       / CHAT#<chatSessionId>          title, createdAt, updatedAt, messageCount,
                                                                   tokenCount, cost
  ChatMessage  CHAT#<chatSessionId> / MSG#<sentAt>#<messageId>     role, text; assistant: tools, tokenCount, cost

A session is owned by its key (USER#<userId>), so a lookup by someone else misses. Messages don't carry the
userId: they are read only after the session lookup succeeds, like a project's contents in the API.

Each message is written in one transaction with an update of its session that requires the session to exist.
A session deleted while its turn was running makes that transaction fail, so no message outlives its session.
The transaction is a Put and a conditional Update; it needs no ConditionCheckItem.

IDs are UUIDv7, so CHAT#<id> sorts by creation and MSG#<sentAt>#<id> by time.
"""

import decimal
import functools
import json
import os
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from contracts import events, logs

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ.get("TABLE_NAME", "interlixir-data")
BUS_NAME = os.environ.get("EVENT_BUS_NAME", "interlixir-bus")

TIMEOUTS = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "max_attempts": 3})

CHAT_PREFIX = "CHAT#"
MSG_PREFIX = "MSG#"
MAX_SESSIONS_LISTED = 50
TITLE_CHARS = 60


class NotFound(Exception):
    """Missing, or someone else's session."""


class Gone(Exception):
    """The session was deleted while a turn was being written to it."""


@functools.cache
def table():
    return boto3.resource("dynamodb", region_name=REGION, config=TIMEOUTS).Table(TABLE_NAME)


@functools.cache
def events_client():
    return boto3.client("events", region_name=REGION, config=TIMEOUTS)


def new_id():
    return str(uuid.uuid7())


def now():
    return events.occurred_at()


def title_from(text):
    """The session's title: the first message, on one line, cut at a word near TITLE_CHARS."""
    line = " ".join(text.split())
    if len(line) <= TITLE_CHARS:
        return line
    cut = line[:TITLE_CHARS].rsplit(" ", 1)[0] or line[:TITLE_CHARS]
    return cut.rstrip(" ,.;:") + "…"


def _session_key(user_id, session_id):
    return {"PK": f"USER#{user_id}", "SK": f"{CHAT_PREFIX}{session_id}"}


def _query_all(**kwargs):
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- Sessions ---------------------------------------------------------------------------------------


def create_session(user_id, first_message):
    session_id = new_id()
    created = now()
    item = {
        **_session_key(user_id, session_id),
        "title": title_from(first_message),
        "createdAt": created,
        "updatedAt": created,
        "messageCount": 0,
        "tokenCount": 0,
        "cost": decimal.Decimal(0),
    }
    table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return item


def get_session(user_id, session_id):
    item = table().get_item(Key=_session_key(user_id, session_id)).get("Item")
    if item is None:
        raise NotFound
    return item


def list_sessions(user_id):
    """The user's sessions, most recently used first (at most MAX_SESSIONS_LISTED)."""
    items = _query_all(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with(CHAT_PREFIX)
    )
    items.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
    return items[:MAX_SESSIONS_LISTED]


def list_messages(user_id, session_id):
    get_session(user_id, session_id)  # the ownership check
    return _query_all(KeyConditionExpression=Key("PK").eq(f"{CHAT_PREFIX}{session_id}"))


def delete_session(user_id, session_id):
    """The session first (it leaves the list at once, and a turn still running can no longer write), then its
    messages."""
    client = table().meta.client
    try:
        table().delete_item(Key=_session_key(user_id, session_id), ConditionExpression="attribute_exists(PK)")
    except client.exceptions.ConditionalCheckFailedException:
        raise NotFound from None
    messages = _query_all(
        KeyConditionExpression=Key("PK").eq(f"{CHAT_PREFIX}{session_id}"), ProjectionExpression="PK, SK"
    )
    with table().batch_writer() as batch:
        for item in messages:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return len(messages)


# --- Messages ---------------------------------------------------------------------------------------


def add_message(user_id, session_id, role, text, tokens=0, cost=0.0, tools=None):
    """Write one message and count it on its session, atomically. Returns the message item; raises Gone if the
    session no longer exists."""
    message_id, sent_at = new_id(), now()
    item = {"PK": f"{CHAT_PREFIX}{session_id}", "SK": f"{MSG_PREFIX}{sent_at}#{message_id}", "role": role,
            "text": text, "sentAt": sent_at}
    if role == "assistant":
        item.update(tokenCount=tokens, cost=decimal.Decimal(str(cost)), tools=tools or [])
    client = table().meta.client
    try:
        client.transact_write_items(TransactItems=[
            {"Put": {"TableName": TABLE_NAME, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": _session_key(user_id, session_id),
                "UpdateExpression": "SET updatedAt = :now ADD messageCount :one, tokenCount :tokens, cost :cost",
                "ConditionExpression": "attribute_exists(PK)",
                "ExpressionAttributeValues": {":now": sent_at, ":one": 1, ":tokens": tokens,
                                              ":cost": decimal.Decimal(str(cost))},
            }},
        ])
    except client.exceptions.TransactionCanceledException:
        raise Gone from None
    return {**item, "messageId": message_id}


def message_view(item):
    """A message as the client and the model history see it."""
    sent_at, _, message_id = item["SK"].removeprefix(MSG_PREFIX).partition("#")
    view = {"messageId": message_id, "role": item["role"], "text": item.get("text", ""), "sentAt": sent_at}
    if item["role"] == "assistant":
        view.update(tokenCount=int(item.get("tokenCount", 0)), cost=float(item.get("cost", 0)),
                    tools=item.get("tools", []))
    return view


def session_view(item):
    return {
        "sessionId": item["SK"].removeprefix(CHAT_PREFIX),
        "title": item.get("title"),
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
        "messageCount": int(item.get("messageCount", 0)),
        "tokenCount": int(item.get("tokenCount", 0)),
        "cost": float(item.get("cost", 0)),
    }


# --- Event --------------------------------------------------------------------------------------------


def publish_turn(user_id, session_id, message_id, tokens, cost):
    """One ChatSession event per answered message: the history writer turns it into a history row with the turn's
    tokens and cost. eventId is the assistant message's ID, so a repeat can't add a second row. A failure is
    logged, never raised: the answer has been saved and shown already."""
    detail = {"eventId": message_id, "userId": user_id, "occurredAt": now(), "chatSessionId": session_id,
              "tokenCount": int(tokens), "cost": round(float(cost), 6)}
    try:
        response = events_client().put_events(Entries=[events.put_events_entry("ChatSession", detail, BUS_NAME)])
        if response.get("FailedEntryCount"):
            raise RuntimeError(json.dumps(response.get("Entries", []))[:500])
    except Exception as error:
        logs.error("ChatSession event not published", userId=user_id, chatSessionId=session_id, error=repr(error))
