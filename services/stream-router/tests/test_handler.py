"""Unit tests for the stream router. Run with `make test-stream-router` (needs jsonschema and boto3)."""

import copy
import json
import pathlib
import unittest
from unittest import mock

from contracts import events

import handler

FILTERS = json.loads((pathlib.Path(handler.__file__).with_name("filters.json")).read_text())["Filters"]

USER = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
PROJECT = "01JA3M7Z0B6H8C2D4E5F6G7H8J"
DOC = "01JA3MC4R5S6T7U8V9W0X1Y2Z3"
JOB = "01JA3M8Q2V7K9XG4T5N6P1R2S3"
REC = "01JA3M8P9W1X2Y3Z4A5B6C7D8E"


def s(value):
    return {"S": value}


def record(event_name, keys, old=None, new=None, event_id="c4ca4238a0b923820dcc509a6f75849b"):
    """A stream record as Lambda delivers it (NEW_AND_OLD_IMAGES)."""
    change = {"ApproximateCreationDateTime": 1792030583.0, "Keys": keys, "StreamViewType": "NEW_AND_OLD_IMAGES"}
    if old is not None:
        change["OldImage"] = {**keys, **old}
    if new is not None:
        change["NewImage"] = {**keys, **new}
    return {"eventID": event_id, "eventName": event_name, "eventSource": "aws:dynamodb", "dynamodb": change}


def project_remove():
    return record("REMOVE", {"PK": s(f"USER#{USER}"), "SK": s(f"PROJECT#{PROJECT}")}, old={"name": s("Pilot")})


def document_remove(**old):
    attrs = {"userId": s(USER), "documentTitle": s("Notes"), "jobId": s(JOB), "recordingId": s(REC)}
    attrs.update(old)
    attrs = {k: v for k, v in attrs.items() if v is not None}
    return record("REMOVE", {"PK": s(f"PROJECT#{PROJECT}"), "SK": s(f"DOC#{DOC}")}, old=attrs)


def job_modify(old_stage="Failed", new_stage="Queued"):
    job = {
        "userId": s(USER),
        "recordingId": s(REC),
        "GSI1PK": s(f"PROJECT#{PROJECT}"),
        "GSI1SK": s(f"JOB#{JOB}"),
    }
    return record(
        "MODIFY",
        {"PK": s(f"JOB#{JOB}"), "SK": s("META")},
        old={**job, "stage": s(old_stage), "retryCount": {"N": "0"}},
        new={**job, "stage": s(new_stage), "retryCount": {"N": "1"}},
    )


def delivered(detail_type, detail):
    """The routed event in the shape EventBridge delivers it, for validate_event()."""
    return {"source": events.SOURCE, "detail-type": detail_type, "detail": detail}


def matches(pattern, value):
    """Event pattern matching for the subset filters.json uses: nested objects, exact values, prefix."""
    if isinstance(pattern, dict):
        return isinstance(value, dict) and all(k in value and matches(p, value[k]) for k, p in pattern.items())
    return any(v == value if not isinstance(v, dict) else isinstance(value, str) and value.startswith(v["prefix"])
               for v in pattern)


def passes_filters(rec):
    return any(matches(json.loads(f["Pattern"]), rec) for f in FILTERS)


class ToEvent(unittest.TestCase):
    def test_project_remove(self):
        detail_type, detail = handler.to_event(project_remove())
        self.assertEqual(detail_type, "DeleteRequested")
        self.assertEqual(detail["entity"], "Project")
        self.assertEqual((detail["userId"], detail["projectId"]), (USER, PROJECT))
        self.assertNotIn("documentId", detail)
        events.validate_event(delivered(detail_type, detail))

    def test_document_remove(self):
        detail_type, detail = handler.to_event(document_remove())
        self.assertEqual(detail_type, "DeleteRequested")
        self.assertEqual((detail["entity"], detail["projectId"], detail["documentId"]), ("Document", PROJECT, DOC))
        self.assertEqual(detail["userId"], USER)
        self.assertEqual((detail["jobId"], detail["recordingId"]), (JOB, REC))
        events.validate_event(delivered(detail_type, detail))

    def test_document_without_job_or_recording(self):
        # Optional: GC falls back to documentId (= jobId) and reads recordingId from the job.
        detail_type, detail = handler.to_event(document_remove(jobId=None, recordingId=None))
        self.assertNotIn("jobId", detail)
        self.assertNotIn("recordingId", detail)
        events.validate_event(delivered(detail_type, detail))

    def test_document_without_user_id_is_skipped(self):
        self.assertIsNone(handler.to_event(document_remove(userId=None)))

    def test_retry(self):
        detail_type, detail = handler.to_event(job_modify())
        self.assertEqual(detail_type, "RetryRequested")
        self.assertEqual(
            (detail["jobId"], detail["projectId"], detail["recordingId"], detail["retryCount"]), (JOB, PROJECT, REC, 1)
        )
        events.validate_event(delivered(detail_type, detail))

    def test_other_stage_changes_are_ignored(self):
        self.assertIsNone(handler.to_event(job_modify("Transcribing", "Redacting")))
        self.assertIsNone(handler.to_event(job_modify("Queued", "Failed")))

    def test_other_removes_are_ignored(self):
        rec = record("REMOVE", {"PK": s(f"PROJECT#{PROJECT}"), "SK": s(f"REC#{REC}")}, old={})
        self.assertIsNone(handler.to_event(rec))

    def test_event_id_and_time_come_from_the_record(self):
        _, first = handler.to_event(project_remove())
        _, again = handler.to_event(copy.deepcopy(project_remove()))
        self.assertEqual(first, again)
        self.assertEqual(first["eventId"], "c4ca4238a0b923820dcc509a6f75849b")
        self.assertEqual(first["occurredAt"], "2026-10-15T02:16:23.000Z")


class Filters(unittest.TestCase):
    def test_passes_what_the_router_handles(self):
        for rec in (project_remove(), document_remove(), job_modify()):
            self.assertTrue(passes_filters(rec), rec["dynamodb"]["Keys"])

    def test_blocks_everything_else(self):
        blocked = [
            job_modify("Transcribing", "Redacting"),
            job_modify("Queued", "Failed"),
            record("INSERT", {"PK": s(f"USER#{USER}"), "SK": s(f"PROJECT#{PROJECT}")}, new={}),
            record("MODIFY", {"PK": s(f"USER#{USER}"), "SK": s(f"PROJECT#{PROJECT}")}, old={}, new={}),
            # renames and Markdown edits: nothing to route
            record("MODIFY", {"PK": s(f"PROJECT#{PROJECT}"), "SK": s(f"DOC#{DOC}")}, old={}, new={}),
            record("REMOVE", {"PK": s(f"PROJECT#{PROJECT}"), "SK": s(f"REC#{REC}")}, old={}),
            record("REMOVE", {"PK": s(f"JOB#{JOB}"), "SK": s("META")}, old={"stage": s("Failed")}),
        ]
        for rec in blocked:
            self.assertFalse(passes_filters(rec), (rec["eventName"], rec["dynamodb"]["Keys"]))


class Handler(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.put_events.side_effect = lambda Entries: {
            "FailedEntryCount": 0,
            "Entries": [{"EventId": str(i)} for i, _ in enumerate(Entries)],
        }
        patcher = mock.patch.object(handler, "_events_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_entries_go_to_the_bus(self):
        result = handler.handler({"Records": [project_remove(), job_modify()]}, None)
        self.assertEqual(result, {"routed": 2})
        (call,) = self.client.put_events.call_args_list
        entries = call.kwargs["Entries"]
        self.assertEqual([e["DetailType"] for e in entries], ["DeleteRequested", "RetryRequested"])
        self.assertTrue(all(e["EventBusName"] == handler.BUS_NAME and e["Source"] == events.SOURCE for e in entries))

    def test_put_events_is_batched_by_ten(self):
        records = [record("REMOVE", project_remove()["dynamodb"]["Keys"], old={}, event_id=f"e{i}") for i in range(23)]
        handler.handler({"Records": records}, None)
        self.assertEqual([len(c.kwargs["Entries"]) for c in self.client.put_events.call_args_list], [10, 10, 3])

    def test_nothing_routed_means_no_call(self):
        handler.handler({"Records": [job_modify("Queued", "Transcribing")]}, None)
        self.client.put_events.assert_not_called()

    def test_partial_failure_raises(self):
        self.client.put_events.side_effect = lambda Entries: {
            "FailedEntryCount": 1,
            "Entries": [{"ErrorCode": "InternalFailure", "ErrorMessage": "x"}],
        }
        with self.assertRaises(RuntimeError):
            handler.handler({"Records": [project_remove()]}, None)


if __name__ == "__main__":
    unittest.main()
