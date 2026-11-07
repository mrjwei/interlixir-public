"""Event contract for the A3 custom bus. The schema is events.schema.json beside this file.

Producers build PutEvents entries with put_events_entry(). It doesn't validate, so Lambda zips need
nothing beyond boto3; producers' unit tests check their output with validate_event() instead.
"""

import datetime
import json
import pathlib

SOURCE = "interlixir.app"

# Recorded as Event items by the history writer (the data-model constraints).
HISTORY_DETAIL_TYPES = ("UploadReceived", "TranscriptionDone", "SynthesisDone", "JobFailed", "ChatSession")
# Control events: they trigger work but aren't user history.
CONTROL_DETAIL_TYPES = ("StageChanged", "DeleteRequested", "RetryRequested")
DETAIL_TYPES = HISTORY_DETAIL_TYPES + CONTROL_DETAIL_TYPES

SCHEMA_PATH = pathlib.Path(__file__).with_name("events.schema.json")


def occurred_at(when=None):
    """Format a time as the contract's occurredAt: UTC, milliseconds, fixed width."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def put_events_entry(detail_type, detail, bus_name):
    """One entry for events.put_events(Entries=[...]). Check FailedEntryCount on the response."""
    if detail_type not in DETAIL_TYPES:
        raise ValueError(f"unknown detail-type: {detail_type}")
    return {"Source": SOURCE, "DetailType": detail_type, "Detail": json.dumps(detail), "EventBusName": bus_name}


def validate_event(event):
    """Validate an event in delivered shape ({source, detail-type, detail, ...}). Raises ValidationError.

    Needs jsonschema, so it's for tests and local tooling, not Lambda runtime code.
    """
    from jsonschema import Draft202012Validator

    Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).validate(event)
