"""History writer against moto (DynamoDB). Inputs are the shared contract's example events, delivered the way the
SQS trigger delivers them. Run with `make test-history-writer` (one-time setup: `make venv-history-writer`)."""

import json
import os
import pathlib
import unittest
from decimal import Decimal

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
for _name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[_name] = "testing"
os.environ.pop("AWS_PROFILE", None)

import boto3
from boto3.dynamodb.conditions import Key
from moto import mock_aws

import handler
from contracts import events

EXAMPLES = pathlib.Path(events.__file__).with_name("examples")
USER = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"  # the examples' userId
JOB = "01JA3M8Q2V7K9XG4T5N6P1R2S3"  # the examples' jobId
PROJECT = "01a13e4f-e4a3-7292-9273-c616769610b9"
RECORDING = "01a13e4f-e79a-737d-8c36-3ac5dc0cc797"


def example(name, **detail):
    event = json.loads((EXAMPLES / f"{name}.json").read_text())
    event["detail"].update(detail)
    events.validate_event(event)  # every input here is a valid event unless a test breaks it on purpose
    return event


def delivered(*bodies):
    """An SQS event from the history queue. A str body is passed through as is."""
    return {"Records": [
        {"messageId": f"m{i}", "eventSource": "aws:sqs",
         "eventSourceARN": "arn:aws:sqs:ap-southeast-2:123456789012:interlixir-history",
         "body": body if isinstance(body, str) else json.dumps(body)}
        for i, body in enumerate(bodies)
    ]}


class HistoryWriterTest(unittest.TestCase):
    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        handler._clients.clear()
        ddb = boto3.resource("dynamodb", region_name="ap-southeast-2")
        self.table = ddb.create_table(
            TableName=handler.TABLE_NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        self.table.put_item(Item={
            "PK": f"JOB#{JOB}", "SK": "META", "GSI1PK": f"PROJECT#{PROJECT}", "GSI1SK": f"JOB#{JOB}",
            "userId": USER, "recordingId": RECORDING, "stage": "Done",
        })
        self.table.put_item(Item={"PK": f"PROJECT#{PROJECT}", "SK": f"REC#{RECORDING}", "fileName": "interview-03.m4a"})

    def history(self):
        """The user's Event items as GET /a3/history queries them, newest first."""
        return self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{USER}") & Key("SK").begins_with("EVENT#"),
            ScanIndexForward=False,
        )["Items"]

    def run_handler(self, *bodies):
        result = handler.handler(delivered(*bodies), None)
        self.assertEqual(result, {"batchItemFailures": []})

    # --- what gets written ---

    def test_each_history_type_becomes_one_event_item(self):
        for name in events.HISTORY_DETAIL_TYPES:
            self.run_handler(example(name, eventId=f"evt-{name.lower()}"))
        items = {i["eventType"]: i for i in self.history()}
        self.assertEqual(set(items), set(events.HISTORY_DETAIL_TYPES))
        done = items["SynthesisDone"]
        self.assertEqual(done["SK"], "EVENT#2026-10-15T01:02:03.456Z#evt-synthesisdone")
        self.assertEqual(done["jobId"], JOB)
        self.assertEqual(done["tokenCount"], 31250)
        self.assertEqual(done["cost"], Decimal("0.0412"))  # exact: the body is parsed with Decimal, not float
        self.assertEqual(items["JobFailed"]["stage"], "Synthesising")
        self.assertEqual(items["ChatSession"]["chatSessionId"], "01JA3N2K3L4M5N6P7Q8R9S0T1V")
        self.assertNotIn("jobId", items["ChatSession"])
        self.assertNotIn("tokenCount", items["UploadReceived"])

    def test_job_events_carry_the_project_and_file_name(self):
        self.run_handler(example("UploadReceived"))
        [item] = self.history()
        self.assertEqual(item["projectId"], PROJECT)
        self.assertEqual(item["fileName"], "interview-03.m4a")

    def test_a_job_that_is_gone_is_recorded_without_them(self):
        self.table.delete_item(Key={"PK": f"JOB#{JOB}", "SK": "META"})
        self.run_handler(example("JobFailed"))
        [item] = self.history()
        self.assertNotIn("projectId", item)
        self.assertNotIn("fileName", item)

    def test_another_users_job_is_not_used_for_context(self):
        self.table.update_item(Key={"PK": f"JOB#{JOB}", "SK": "META"}, UpdateExpression="SET userId = :u",
                               ExpressionAttributeValues={":u": "someone-else"})
        self.run_handler(example("UploadReceived"))
        [item] = self.history()
        self.assertNotIn("projectId", item)

    def test_newest_first_with_the_claims_outside_the_history_range(self):
        self.run_handler(example("UploadReceived", eventId="a", occurredAt="2026-10-15T01:00:00.000Z"),
                         example("TranscriptionDone", eventId="b", occurredAt="2026-10-15T01:05:00.000Z"),
                         example("SynthesisDone", eventId="c", occurredAt="2026-10-15T01:06:00.000Z"))
        self.assertEqual([i["eventType"] for i in self.history()], ["SynthesisDone", "TranscriptionDone", "UploadReceived"])
        claims = self.table.query(KeyConditionExpression=Key("PK").eq(f"USER#{USER}") & Key("SK").begins_with("EVENTID#"))
        self.assertEqual(claims["Count"], 3)
        self.assertEqual(claims["Items"][0]["eventSk"], "EVENT#2026-10-15T01:00:00.000Z#a")
        self.assertIsInstance(claims["Items"][0]["expiresAt"], Decimal)  # epoch seconds for the table's TTL

    # --- at least once ---

    def test_redelivery_of_the_same_emission_is_one_item(self):
        event = example("SynthesisDone")
        self.run_handler(event, event)
        self.run_handler(event)
        self.assertEqual(len(self.history()), 1)

    def test_re_emission_with_a_new_occurred_at_is_dropped(self):
        # A producer that crashed after PutEvents re-emits the same eventId, stamped later.
        self.run_handler(example("JobFailed", eventId="jobfailed-x-0", occurredAt="2026-10-15T01:00:00.000Z"))
        self.run_handler(example("JobFailed", eventId="jobfailed-x-0", occurredAt="2026-10-15T01:00:09.000Z"))
        [item] = self.history()
        self.assertEqual(item["SK"], "EVENT#2026-10-15T01:00:00.000Z#jobfailed-x-0")  # the first time is kept

    def test_a_crash_between_claim_and_write_is_finished_by_the_redelivery(self):
        event = example("TranscriptionDone")
        real = handler.write_event
        handler.write_event = lambda item: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            result = handler.handler(delivered(event), None)
        finally:
            handler.write_event = real
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m0"}]})
        self.assertEqual(self.history(), [])
        self.run_handler(event)  # the same message, redelivered: its claim matches, so the write goes ahead
        self.assertEqual(len(self.history()), 1)

    def test_same_event_id_for_two_users_is_two_items(self):
        self.run_handler(example("ChatSession"), example("ChatSession", userId="9f8e7d6c-0000-4000-8000-000000000000"))
        self.assertEqual(len(self.history()), 1)
        other = self.table.query(KeyConditionExpression=Key("PK").eq("USER#9f8e7d6c-0000-4000-8000-000000000000")
                                 & Key("SK").begins_with("EVENT#"))
        self.assertEqual(other["Count"], 1)

    # --- what is dropped, what is retried ---

    def test_control_events_are_dropped(self):
        for name in ("StageChanged", "RetryRequested", "DeleteRequested.project"):
            self.run_handler(example(name))
        self.assertEqual(self.table.scan()["Count"], 2)  # only the fixtures

    def test_invalid_events_are_dropped_not_retried(self):
        def broken(name, **change):
            event = example(name)
            event["detail"].update(change)
            return event

        no_cost = example("SynthesisDone")
        del no_cost["detail"]["cost"]
        bad = [
            {**example("UploadReceived"), "source": "someone.else"},
            broken("UploadReceived", userId="USER#x"),  # would reach into another key range
            broken("UploadReceived", occurredAt="2026-10-15T01:02:03Z"),  # not fixed width: breaks the sort
            broken("JobFailed", stage="Done"),
            broken("ChatSession", tokenCount=-1),
            no_cost,
            "not json",
            json.dumps([1, 2]),
        ]
        self.run_handler(*bad)
        self.assertEqual(self.history(), [])

    def test_an_aws_error_fails_only_that_record(self):
        good, bad = example("UploadReceived", eventId="ok"), example("UploadReceived", eventId="boom")
        real = handler.claim

        def claim(item):
            if item["eventId"] == "boom":
                raise RuntimeError("throttled")
            return real(item)

        handler.claim = claim
        try:
            result = handler.handler(delivered(good, bad), None)
        finally:
            handler.claim = real
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m1"}]})
        self.assertEqual(len(self.history()), 1)

    def test_direct_eventbridge_invocation_with_float_cost(self):
        handler.handler(example("SynthesisDone"), None)  # Lambda parses a direct event with floats
        [item] = self.history()
        self.assertEqual(item["cost"], Decimal("0.0412"))


if __name__ == "__main__":
    unittest.main()
