"""Dead-letter handler against moto (DynamoDB). Events are captured and checked against the shared contract.
Run with `make test-dlq-handler` (one-time setup: `make venv-dlq-handler`)."""

import json
import os
import unittest

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
for _name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[_name] = "testing"
os.environ.pop("AWS_PROFILE", None)

import boto3
from moto import mock_aws

import handler
from contracts import events

USER = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
OTHER = "9f8e7d6c-0000-4000-8000-000000000000"
PROJECT = "01a13e4f-e4a3-7292-9273-c616769610b9"
RECORDING = "01a13e4f-e79a-737d-8c36-3ac5dc0cc797"
JOB = "01a13e4f-e79a-737d-8c36-3ac634026915"
KEY = f"recordings/{USER}/{PROJECT}/{RECORDING}.m4a"
ARN = "arn:aws:sqs:ap-southeast-2:123456789012:interlixir-{}-dlq"


class FakeEvents:
    """Stands in for the EventBridge client: records entries, can fail the next call."""

    def __init__(self):
        self.entries, self.fail_next = [], False

    def put_events(self, Entries):
        if self.fail_next:
            self.fail_next = False
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]}
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "x"} for _ in Entries]}

    def delivered(self):
        out = []
        for entry in self.entries:
            event = {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": json.loads(entry["Detail"])}
            events.validate_event(event)
            out.append(event)
        return out


def dead_letter(queue, *bodies):
    """An SQS event from one dead-letter queue. A str body is passed through as is."""
    return {"Records": [
        {"messageId": f"m{i}", "eventSource": "aws:sqs", "eventSourceARN": ARN.format(queue),
         "body": body if isinstance(body, str) else json.dumps(body)}
        for i, body in enumerate(bodies)
    ]}


def s3_notification(key=KEY, bucket=handler.UPLOADS_BUCKET):
    return {"Records": [{"eventSource": "aws:s3", "eventName": "ObjectCreated:Post",
                         "s3": {"bucket": {"name": bucket}, "object": {"key": key.replace(" ", "+"), "size": 1234}}}]}


def retry_requested(retry_count=1):
    return {"version": "0", "source": events.SOURCE, "detail-type": "RetryRequested",
            "detail": {"eventId": "e1", "userId": USER, "occurredAt": "2026-10-16T01:02:03.456Z", "jobId": JOB,
                       "projectId": PROJECT, "recordingId": RECORDING, "retryCount": retry_count}}


def chunk(n=2, total=6, user=USER):
    return {"jobId": JOB, "userId": user, "chunk": n, "chunksTotal": total, "startSeconds": n * 300}


class DeadLetterTest(unittest.TestCase):
    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        handler._clients.clear()
        self.fake_events = FakeEvents()
        handler._clients["events"] = self.fake_events
        ddb = boto3.resource("dynamodb", region_name="ap-southeast-2")
        self.table = ddb.create_table(
            TableName=handler.TABLE_NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        self.table.put_item(Item={
            "PK": f"PROJECT#{PROJECT}", "SK": f"REC#{RECORDING}", "storageKey": KEY, "jobId": JOB,
        })
        self.set_job(stage="Queued", retryCount=0)

    def set_job(self, **values):
        self.table.put_item(Item={
            "PK": f"JOB#{JOB}", "SK": "META", "GSI1PK": f"PROJECT#{PROJECT}", "GSI1SK": f"JOB#{JOB}",
            "userId": USER, "recordingId": RECORDING, **values,
        })

    def job(self):
        return self.table.get_item(Key={"PK": f"JOB#{JOB}", "SK": "META"})["Item"]

    def assert_failed(self, stage, retry_count=0, reason_has=""):
        job = self.job()
        self.assertEqual(job["stage"], "Failed")
        self.assertIn(reason_has, job["failureReason"])
        [event] = self.fake_events.delivered()
        self.assertEqual(event["detail-type"], "JobFailed")
        self.assertEqual(event["detail"]["eventId"], f"jobfailed-{JOB}-{retry_count}")
        self.assertEqual(event["detail"]["stage"], stage)
        self.assertEqual(event["detail"]["userId"], USER)

    def assert_unchanged(self, stage):
        self.assertEqual(self.job()["stage"], stage)
        self.assertNotIn("failureReason", self.job())
        self.assertEqual(self.fake_events.entries, [])

    # --- upload DLQ ---

    def test_upload_notification_fails_the_queued_job(self):
        result = handler.handler(dead_letter("upload", s3_notification()), None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assert_failed("Queued", reason_has="prepared for transcription")

    def test_upload_notification_for_a_job_that_moved_on_changes_nothing(self):
        self.set_job(stage="Transcribing", retryCount=0, chunksTotal=6)
        handler.handler(dead_letter("upload", s3_notification()), None)
        self.assert_unchanged("Transcribing")

    def test_upload_notification_for_a_retried_job_changes_nothing(self):
        self.set_job(stage="Queued", retryCount=1)  # only RetryRequested messages belong to retryCount 1
        handler.handler(dead_letter("upload", s3_notification()), None)
        self.assert_unchanged("Queued")

    def test_upload_notification_without_a_recording_item_is_dropped(self):
        self.table.delete_item(Key={"PK": f"PROJECT#{PROJECT}", "SK": f"REC#{RECORDING}"})
        result = handler.handler(dead_letter("upload", s3_notification()), None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assert_unchanged("Queued")

    def test_other_bucket_or_key_is_ignored(self):
        handler.handler(dead_letter("upload", s3_notification(bucket="someone-else"),
                                    s3_notification(key=f"context/{USER}/{PROJECT}/x.pdf")), None)
        self.assert_unchanged("Queued")

    def test_retry_requested_fails_the_job_at_that_retry_count(self):
        self.set_job(stage="Queued", retryCount=1)
        handler.handler(dead_letter("upload", retry_requested(1)), None)
        self.assert_failed("Queued", retry_count=1)

    def test_stale_retry_requested_changes_nothing(self):
        self.set_job(stage="Queued", retryCount=2)
        handler.handler(dead_letter("upload", retry_requested(1)), None)
        self.assert_unchanged("Queued")

    def test_s3_test_event_is_dropped(self):
        result = handler.handler(dead_letter("upload", {"Event": "s3:TestEvent", "Bucket": "x"}), None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assert_unchanged("Queued")

    # --- transcription DLQ ---

    def test_chunk_fails_a_transcribing_job_and_names_the_part(self):
        self.set_job(stage="Transcribing", retryCount=0, chunksTotal=6, doneChunks={0, 1})
        handler.handler(dead_letter("transcription", chunk(2, 6)), None)
        self.assert_failed("Transcribing", reason_has="Part 3 of 6")
        self.assertEqual(self.job()["doneChunks"], {0, 1})  # left for the retry to reconcile with S3

    def test_chunk_from_an_older_chunking_changes_nothing(self):
        self.set_job(stage="Transcribing", retryCount=1, chunksTotal=5)
        handler.handler(dead_letter("transcription", chunk(2, 6)), None)
        self.assert_unchanged("Transcribing")

    def test_chunk_for_another_users_job_changes_nothing(self):
        self.set_job(stage="Transcribing", retryCount=0, chunksTotal=6)
        handler.handler(dead_letter("transcription", chunk(2, 6, user=OTHER)), None)
        self.assert_unchanged("Transcribing")

    def test_chunk_for_a_done_job_changes_nothing(self):
        self.set_job(stage="Done", retryCount=0, chunksTotal=6)
        handler.handler(dead_letter("transcription", chunk(2, 6)), None)
        self.assert_unchanged("Done")

    def test_second_chunk_of_the_same_job_is_a_no_op(self):
        self.set_job(stage="Transcribing", retryCount=0, chunksTotal=6)
        handler.handler(dead_letter("transcription", chunk(2, 6), chunk(4, 6)), None)
        self.assert_failed("Transcribing", reason_has="Part 3 of 6")  # one event, the first part's reason

    # --- synthesis DLQ ---

    def test_synthesis_request_fails_a_working_job(self):
        for stage in handler.WORKING:
            with self.subTest(stage=stage):
                self.fake_events.entries.clear()
                self.set_job(stage=stage, retryCount=0, chunksTotal=6)
                handler.handler(dead_letter("synthesis", {"jobId": JOB, "userId": USER}), None)
                self.assert_failed(stage, reason_has="document couldn't be written")

    def test_synthesis_request_for_a_failed_job_changes_nothing(self):
        self.set_job(stage="Failed", retryCount=0, failureReason="earlier")
        handler.handler(dead_letter("synthesis", {"jobId": JOB, "userId": USER}), None)
        self.assertEqual(self.job()["failureReason"], "earlier")
        self.assertEqual(self.fake_events.entries, [])

    def test_job_that_is_gone_is_dropped(self):
        self.table.delete_item(Key={"PK": f"JOB#{JOB}", "SK": "META"})
        result = handler.handler(dead_letter("synthesis", {"jobId": JOB, "userId": USER}), None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(self.fake_events.entries, [])

    # --- bad messages, errors ---

    def test_unreadable_or_foreign_bodies_are_dropped(self):
        result = handler.handler(dead_letter(
            "transcription", "not json", {"jobId": JOB}, chunk(6, 6), {"jobId": "bad#id", "userId": USER,
                                                                      "chunk": 0, "chunksTotal": 1},
        ), None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(self.fake_events.entries, [])

    def test_failed_put_events_leaves_the_record_and_the_job(self):
        self.set_job(stage="Synthesising", retryCount=0, chunksTotal=6)
        self.fake_events.fail_next = True
        result = handler.handler(dead_letter("synthesis", {"jobId": JOB, "userId": USER}), None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m0"}]})
        self.assertEqual(self.job()["stage"], "Synthesising")
        handler.handler(dead_letter("synthesis", {"jobId": JOB, "userId": USER}), None)  # the redelivery
        self.assert_failed("Synthesising")

    def test_job_that_moves_between_read_and_write_is_left_alone(self):
        self.set_job(stage="Synthesising", retryCount=0, chunksTotal=6)
        real_get = handler._get

        def get_then_finish(pk, sk):
            item = real_get(pk, sk)
            if pk.startswith("JOB#"):
                self.set_job(stage="Done", retryCount=0, chunksTotal=6)  # synthesis finished meanwhile
            return item

        handler._get = get_then_finish
        self.addCleanup(setattr, handler, "_get", real_get)
        result = handler.handler(dead_letter("synthesis", {"jobId": JOB, "userId": USER}), None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(self.job()["stage"], "Done")


if __name__ == "__main__":
    unittest.main()
