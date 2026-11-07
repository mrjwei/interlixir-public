"""Contract tests for the A3 event schema. Run with `make test-contracts`."""

import copy
import datetime
import json
import pathlib
import unittest

from jsonschema import Draft202012Validator, ValidationError

from contracts import events

EXAMPLES = pathlib.Path(events.__file__).with_name("examples")


def example(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


def with_detail(name, **changes):
    """An example with detail fields changed; a value of None removes the field."""
    event = copy.deepcopy(example(name))
    for key, value in changes.items():
        if value is None:
            event["detail"].pop(key, None)
        else:
            event["detail"][key] = value
    return event


class SchemaTest(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(json.loads(events.SCHEMA_PATH.read_text()))

    def test_schema_and_module_agree(self):
        schema = json.loads(events.SCHEMA_PATH.read_text())
        self.assertEqual(schema["properties"]["source"]["const"], events.SOURCE)
        self.assertEqual(schema["properties"]["detail-type"]["enum"], list(events.DETAIL_TYPES))
        self.assertTrue(set(events.DETAIL_TYPES) <= schema["$defs"].keys())


class ExamplesTest(unittest.TestCase):
    def test_every_example_is_valid(self):
        for file in sorted(EXAMPLES.glob("*.json")):
            with self.subTest(file.name):
                events.validate_event(json.loads(file.read_text()))

    def test_every_detail_type_has_an_example(self):
        covered = {json.loads(f.read_text())["detail-type"] for f in EXAMPLES.glob("*.json")}
        self.assertEqual(covered, set(events.DETAIL_TYPES))


class RejectsTest(unittest.TestCase):
    def assertInvalid(self, event):
        with self.assertRaises(ValidationError):
            events.validate_event(event)

    def test_wrong_source(self):
        event = example("UploadReceived")
        event["source"] = "aws.s3"
        self.assertInvalid(event)

    def test_unknown_detail_type(self):
        event = example("UploadReceived")
        event["detail-type"] = "JobDeleted"
        self.assertInvalid(event)

    def test_missing_user_id(self):
        self.assertInvalid(with_detail("SynthesisDone", userId=None))

    def test_unknown_detail_field(self):
        self.assertInvalid(with_detail("UploadReceived", projectName="Interviews"))

    def test_detail_checked_against_its_own_type(self):
        event = example("SynthesisDone")
        event["detail-type"] = "ChatSession"  # jobId instead of chatSessionId
        self.assertInvalid(event)

    def test_id_with_key_separator(self):
        self.assertInvalid(with_detail("UploadReceived", jobId="JOB#123"))
        self.assertInvalid(with_detail("UploadReceived", jobId="a/b"))

    def test_occurred_at_must_be_utc_milliseconds(self):
        self.assertInvalid(with_detail("UploadReceived", occurredAt="2026-10-15T01:02:03Z"))
        self.assertInvalid(with_detail("UploadReceived", occurredAt="2026-10-15T11:02:03.456+10:00"))

    def test_negative_tokens_or_cost(self):
        self.assertInvalid(with_detail("SynthesisDone", tokenCount=-1))
        self.assertInvalid(with_detail("SynthesisDone", cost=-0.01))

    def test_job_failed_stage_is_not_terminal(self):
        self.assertInvalid(with_detail("JobFailed", stage="Done"))
        self.assertInvalid(with_detail("JobFailed", stage="Failed"))

    def test_document_delete_needs_document_id(self):
        self.assertInvalid(with_detail("DeleteRequested.document", documentId=None))

    def test_project_delete_has_no_document_id(self):
        self.assertInvalid(with_detail("DeleteRequested.project", documentId="01JA3MC4R5S6T7U8V9W0X1Y2Z3"))

    def test_project_delete_has_no_job_or_recording(self):
        # M20: only a removed Document names its job and recording; GC finds a project's jobs through GSI1.
        self.assertInvalid(with_detail("DeleteRequested.project", jobId="01JA3M8Q2V7K9XG4T5N6P1R2S3"))
        self.assertInvalid(with_detail("DeleteRequested.project", recordingId="01JA3M8P9W1X2Y3Z4A5B6C7D8E"))

    def test_document_delete_job_and_recording_are_optional(self):
        # Documents written before M20 by an older synthesis may lack recordingId; GC reads it from the job then.
        events.validate_event(with_detail("DeleteRequested.document", jobId=None, recordingId=None))

    def test_retry_count_starts_at_one(self):
        self.assertInvalid(with_detail("RetryRequested", retryCount=0))


class HelpersTest(unittest.TestCase):
    def test_occurred_at_format(self):
        brisbane = datetime.timezone(datetime.timedelta(hours=10))
        when = datetime.datetime(2026, 10, 15, 11, 2, 3, 456789, tzinfo=brisbane)
        self.assertEqual(events.occurred_at(when), "2026-10-15T01:02:03.456Z")

    def test_occurred_at_default_is_valid(self):
        events.validate_event(with_detail("UploadReceived", occurredAt=events.occurred_at()))

    def test_put_events_entry_round_trip(self):
        sent = example("RetryRequested")
        entry = events.put_events_entry("RetryRequested", sent["detail"], "interlixir-bus")
        self.assertEqual(entry["Source"], events.SOURCE)
        self.assertEqual(entry["EventBusName"], "interlixir-bus")
        # What a target receives: EventBridge parses Detail back into an object.
        events.validate_event({"source": entry["Source"], "detail-type": entry["DetailType"],
                               "detail": json.loads(entry["Detail"])})

    def test_put_events_entry_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            events.put_events_entry("JobDeleted", {}, "interlixir-bus")


if __name__ == "__main__":
    unittest.main()
