"""Whisper worker against moto (S3, DynamoDB, SQS). Whisper itself is faked here (test_transcriber.py and
bench.py run the real model). Events, synthesis messages and transcripts are checked against the shared
contracts."""

import io
import json
import os
import pathlib
import time
import unittest
import urllib.error
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
for _name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[_name] = "testing"
os.environ.pop("AWS_PROFILE", None)
os.environ.pop("ECS_AGENT_URI", None)

import boto3
from moto import mock_aws

import worker
from contracts import events, logs, messages, transcripts
from transcriber import Stopped

USER = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
OTHER = "9f8e7d6c-0000-4000-8000-000000000000"
JOB = "01a13e4f-e79a-737d-8c36-3ac634026915"
BUCKET = worker.UPLOADS_BUCKET


class FakeEvents:
    """Stands in for the EventBridge client: records entries, can fail the next N calls."""

    def __init__(self):
        self.entries, self.fail_next = [], 0

    def put_events(self, Entries):
        if self.fail_next:
            self.fail_next -= 1
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


class FakeTranscriber:
    """Returns one segment per chunk; can raise, or behave as if SIGTERM arrived mid-chunk."""

    name, beam_size, cpu_threads = "distil-small.en", 5, 0

    def __init__(self, error=None, sigterm=False, seconds=0.0):
        self.error, self.sigterm, self.seconds, self.calls = error, sigterm, seconds, []

    def transcribe(self, path, start_seconds=0, should_stop=lambda: False):
        self.calls.append((pathlib.Path(path).read_bytes(), start_seconds))
        time.sleep(self.seconds)
        if self.error:
            raise self.error
        if self.sigterm:
            worker.stopping.set()
        if should_stop():
            raise Stopped()
        segment = {"start": start_seconds + 0.5, "end": start_seconds + 4.25, "text": "Hello there."}
        return {"audioSeconds": 300.0, "text": "Hello there.", "segments": [segment]}


class FakeProtection:
    def __init__(self, refuse=False):
        self.calls, self.refuse = [], refuse

    def set(self, enabled):
        self.calls.append(enabled)
        return not (enabled and self.refuse)


def chunk_message(n=0, total=1, user=USER, job=JOB):
    return {"jobId": job, "userId": user, "chunk": n, "chunksTotal": total, "startSeconds": n * 300}


class Base(unittest.TestCase):
    """Moto resources plus a Job in Transcribing, as the job creator leaves it. No tests of its own."""

    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        worker._clients.clear()
        worker.stopping.clear()
        self.addCleanup(worker.stopping.clear)
        self.fake_events = FakeEvents()
        worker._clients["events"] = self.fake_events
        self.protection = FakeProtection()
        for name, value in (("WAIT_SECONDS", 0),):
            patcher = mock.patch.object(worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        ddb = boto3.resource("dynamodb", region_name="ap-southeast-2")
        self.table = ddb.create_table(
            TableName=worker.TABLE_NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        self.s3 = boto3.client("s3", region_name="ap-southeast-2")
        self.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
        self.sqs = boto3.client("sqs", region_name="ap-southeast-2")
        self.queues = {}
        for name, attr in (("transcription", "TRANSCRIPTION_QUEUE_URL"), ("synthesis", "SYNTHESIS_QUEUE_URL")):
            url = self.sqs.create_queue(QueueName=f"interlixir-{name}")["QueueUrl"]
            self.queues[name] = url
            patcher = mock.patch.object(worker, attr, url)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- helpers ---

    def make_job(self, total=1, **extra):
        self.table.put_item(Item={
            "PK": f"JOB#{JOB}", "SK": "META", "userId": USER, "stage": "Transcribing", "retryCount": 0,
            "chunksTotal": total, **extra,
        })
        for n in range(total):
            self.s3.put_object(Bucket=BUCKET, Key=f"chunks/{JOB}/{n}.wav", Body=b"RIFF" + bytes([n]))

    def job(self):
        return self.table.get_item(Key={"PK": f"JOB#{JOB}", "SK": "META"}).get("Item")

    def enqueue(self, body):
        return self.sqs.send_message(QueueUrl=self.queues["transcription"], MessageBody=json.dumps(body))["MessageId"]

    def poll(self, transcriber):
        worker.poll_once(transcriber, self.protection)

    def receive(self):
        """The next transcription message as the worker receives it."""
        [message] = self.sqs.receive_message(
            QueueUrl=self.queues["transcription"], MessageSystemAttributeNames=["ApproximateReceiveCount"],
            MessageAttributeNames=[worker.CLAIM_ATTRIBUTE],
        )["Messages"]
        return message

    def counts(self, queue="transcription"):
        """(visible, in flight) for a queue; moto's counts are exact."""
        attrs = self.sqs.get_queue_attributes(
            QueueUrl=self.queues[queue],
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return int(attrs["ApproximateNumberOfMessages"]), int(attrs["ApproximateNumberOfMessagesNotVisible"])

    def drain(self, queue):
        bodies = []
        while True:
            got = self.sqs.receive_message(QueueUrl=self.queues[queue], MaxNumberOfMessages=10).get("Messages", [])
            if not got:
                return bodies
            for message in got:
                bodies.append(json.loads(message["Body"]))
                self.sqs.delete_message(QueueUrl=self.queues[queue], ReceiptHandle=message["ReceiptHandle"])

    def transcript(self, n):
        body = json.loads(self.s3.get_object(Bucket=BUCKET, Key=f"transcripts/{USER}/{JOB}/chunks/{n}.json")["Body"].read())
        transcripts.validate_transcript(body)
        return body

    def transcript_keys(self):
        listed = self.s3.list_objects_v2(Bucket=BUCKET, Prefix="transcripts/").get("Contents", [])
        return sorted(o["Key"] for o in listed)


class WorkerTest(Base):
    def test_one_chunk_job_end_to_end(self):
        self.make_job(total=1)
        message_id = self.enqueue(chunk_message())
        transcriber = FakeTranscriber()
        self.poll(transcriber)

        self.assertEqual(transcriber.calls, [(b"RIFF\x00", 0)])
        doc = self.transcript(0)
        self.assertEqual((doc["jobId"], doc["chunk"], doc["model"], doc["language"]), (JOB, 0, "distil-small.en", "en"))
        job = self.job()
        self.assertEqual(job["doneChunks"], {0})
        self.assertEqual(job["synthesisEnqueued"], message_id)
        self.assertEqual(job["stage"], "Transcribing")  # synthesis moves it on

        requests = self.drain("synthesis")
        self.assertEqual(requests, [{"jobId": JOB, "userId": USER}])
        messages.validate_message("SynthesisRequest", requests[0])
        delivered = self.fake_events.delivered()
        self.assertEqual([e["detail-type"] for e in delivered], ["TranscriptionDone"])
        self.assertEqual(delivered[0]["detail"]["eventId"], f"transcriptiondone-{JOB}-0")

        self.assertEqual(self.counts(), (0, 0))  # deleted
        self.assertEqual(self.protection.calls, [True, False])

    def test_timestamps_are_offset_by_the_chunk_start(self):
        self.make_job(total=3)
        self.enqueue(chunk_message(n=2, total=3))
        self.poll(FakeTranscriber())
        self.assertEqual(self.transcript(2)["segments"], [{"start": 600.5, "end": 604.25, "text": "Hello there."}])
        self.assertEqual(self.transcript(2)["startSeconds"], 600)

    def test_only_the_last_chunk_requests_synthesis(self):
        self.make_job(total=3)
        for n in (2, 0, 1):  # any order
            self.enqueue(chunk_message(n=n, total=3))
            self.poll(FakeTranscriber())
            done = self.job()["doneChunks"]
            if len(done) < 3:
                self.assertNotIn("synthesisEnqueued", self.job())
                self.assertEqual(self.counts("synthesis"), (0, 0))
        self.assertEqual(self.job()["doneChunks"], {0, 1, 2})
        self.assertEqual(len(self.drain("synthesis")), 1)
        self.assertEqual(len(self.fake_events.entries), 1)
        self.assertEqual(self.transcript_keys(), [f"transcripts/{USER}/{JOB}/chunks/{n}.json" for n in range(3)])

    def test_a_chunk_sent_twice_is_transcribed_and_counted_once(self):
        self.make_job(total=2)
        transcriber = FakeTranscriber()
        for n in (0, 0, 1):  # the job creator redid a run and sent chunk 0 again
            self.enqueue(chunk_message(n=n, total=2))
            self.poll(transcriber)
        self.assertEqual(len(transcriber.calls), 2)
        self.assertEqual(self.job()["doneChunks"], {0, 1})
        self.assertEqual(len(self.drain("synthesis")), 1)
        self.assertEqual(self.counts(), (0, 0))

    def test_a_late_duplicate_after_synthesis_was_requested_does_nothing(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        self.poll(FakeTranscriber())
        self.drain("synthesis")

        transcriber = FakeTranscriber()
        self.enqueue(chunk_message())  # a different message for the same chunk
        self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(self.drain("synthesis"), [])
        self.assertEqual(len(self.fake_events.entries), 1)
        self.assertEqual(self.counts(), (0, 0))

    def test_the_winners_redelivery_finishes_its_claim(self):
        self.make_job(total=1)
        message_id = self.enqueue(chunk_message())
        real_send = worker._send
        with mock.patch.object(worker, "RETRY_BACKOFF_SECONDS", 0), \
                mock.patch.object(worker, "_send", side_effect=RuntimeError("SQS down")):
            self.poll(FakeTranscriber())
        self.assertEqual(self.job()["synthesisEnqueued"], message_id)  # claimed, but nothing sent
        self.assertEqual(self.counts(), (1, 0))  # not deleted: back on the queue (no backoff in this test)

        # The same message comes back (same MessageId, new receipt handle).
        transcriber = FakeTranscriber()
        with mock.patch.object(worker, "_send", side_effect=real_send):
            self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])  # the transcript was already written
        self.assertEqual(self.drain("synthesis"), [{"jobId": JOB, "userId": USER}])
        self.assertEqual(len(self.fake_events.entries), 1)
        self.assertEqual(self.counts(), (0, 0))

    def test_a_different_message_cant_take_over_a_claim(self):
        self.make_job(total=1, synthesisEnqueued="some-other-message", doneChunks={0})
        self.s3.put_object(Bucket=BUCKET, Key=f"transcripts/{USER}/{JOB}/chunks/0.json", Body=b"{}")
        self.enqueue(chunk_message())
        self.poll(FakeTranscriber())
        self.assertEqual(self.drain("synthesis"), [])
        self.assertEqual(self.counts(), (0, 0))

    def test_a_failed_event_put_is_retried_through_redelivery(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        self.fake_events.fail_next = 1
        with mock.patch.object(worker, "RETRY_BACKOFF_SECONDS", 0):
            self.poll(FakeTranscriber())
            self.assertEqual(self.counts(), (1, 0))
            self.poll(FakeTranscriber())
        self.assertEqual([e["detail-type"] for e in self.fake_events.delivered()], ["TranscriptionDone"])
        # The request went out on both attempts: synthesis must be idempotent (shared/README.md).
        self.assertEqual(len(self.drain("synthesis")), 2)
        self.assertEqual(self.counts(), (0, 0))

    def test_job_gone(self):
        self.enqueue(chunk_message())  # no Job item: the project was deleted
        transcriber = FakeTranscriber()
        self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])
        self.assertIsNone(self.job())  # the ADD didn't recreate it
        self.assertEqual(self.transcript_keys(), [])
        self.assertEqual(self.counts(), (0, 0))

    def test_another_users_job(self):
        self.make_job(total=1)
        transcriber = FakeTranscriber()
        self.enqueue(chunk_message(user=OTHER))
        self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])
        self.assertNotIn("doneChunks", self.job())
        self.assertEqual(self.counts(), (0, 0))

    def test_message_from_an_older_chunking(self):
        self.make_job(total=6)
        transcriber = FakeTranscriber()
        self.enqueue(chunk_message(n=0, total=3))
        self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])
        self.assertNotIn("doneChunks", self.job())
        self.assertEqual(self.counts(), (0, 0))

    def test_job_deleted_while_transcribing(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())

        class Deleting(FakeTranscriber):
            def transcribe(inner, *args, **kwargs):
                self.table.delete_item(Key={"PK": f"JOB#{JOB}", "SK": "META"})
                return super().transcribe(*args, **kwargs)

        self.poll(Deleting())
        self.assertIsNone(self.job())
        self.assertEqual(self.drain("synthesis"), [])
        self.assertEqual(self.counts(), (0, 0))

    def test_transcription_error_backs_off(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        with mock.patch.object(worker, "release", wraps=worker.release) as release:
            self.poll(FakeTranscriber(error=RuntimeError("model crashed")))
        self.assertEqual(release.call_args.args[1], worker.RETRY_BACKOFF_SECONDS)
        self.assertEqual(self.counts(), (0, 1))
        self.assertEqual(self.transcript_keys(), [])
        self.assertNotIn("doneChunks", self.job())
        self.assertEqual(self.protection.calls, [True, False])

    def test_missing_chunk_audio_backs_off(self):
        self.make_job(total=1)
        self.s3.delete_object(Bucket=BUCKET, Key=f"chunks/{JOB}/0.wav")
        self.enqueue(chunk_message())
        self.poll(FakeTranscriber())
        self.assertEqual(self.counts(), (0, 1))  # retried, then the DLQ (the dead-letter handler marks the job Failed)

    def test_bad_body_is_kept_for_the_dlq(self):
        transcriber = FakeTranscriber()
        for body in ({"jobId": JOB}, {**chunk_message(), "jobId": "a/b"}, {**chunk_message(), "jobId": JOB + "\n"},
                     {**chunk_message(), "chunk": 1}, {**chunk_message(), "chunk": True}, [1]):
            with self.subTest(body=body):
                self.enqueue(body)
                self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(self.counts(), (0, 6))

    def test_sigterm_mid_transcription_releases_the_chunk(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        self.poll(FakeTranscriber(sigterm=True))
        self.assertEqual(self.counts(), (1, 0))  # visible again straight away for another task
        self.assertEqual(self.transcript_keys(), [])
        self.assertNotIn("doneChunks", self.job())
        self.assertEqual(self.protection.calls, [True, False])

    def test_sigterm_during_the_long_poll_releases_without_starting(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        worker.stopping.set()
        transcriber = FakeTranscriber()
        self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(self.protection.calls, [])

    def test_task_picked_for_stopping_releases_without_starting(self):
        # a scale-in picked the task between chunks; it started the next one anyway and SIGTERM
        # abandoned it, costing that message a receive and the work done so far.
        self.make_job(total=1)
        self.enqueue(chunk_message())
        self.protection = FakeProtection(refuse=True)
        transcriber = FakeTranscriber()
        self.poll(transcriber)
        self.assertEqual(transcriber.calls, [])
        self.assertEqual(self.counts(), (1, 0))  # visible again straight away for another task
        self.assertEqual(self.protection.calls, [True])
        self.assertTrue(worker.stopping.is_set())  # main() exits; ECS's SIGTERM finds nothing to abandon

    def test_a_stop_is_not_an_attempt(self):
        # one message was refused by three stopping tasks in a row, reached the DLQ on its 3
        # receives and failed its job without a single attempt.
        self.make_job(total=1)
        first_id = self.enqueue(chunk_message())
        for _ in range(3):
            worker.stopping.clear()
            worker.process(self.receive(), FakeTranscriber(), FakeProtection(refuse=True))
        message = self.receive()
        self.assertNotEqual(message["MessageId"], first_id)
        self.assertEqual(message["Attributes"]["ApproximateReceiveCount"], "1")  # a new message each time
        self.assertEqual(worker.claim_id(message), first_id)
        self.assertEqual(self.counts(), (0, 1))  # the copies replaced it; nothing else left behind

    def test_a_handed_back_copy_keeps_the_claim(self):
        # The winner claims and dies before sending; its redelivery is then refused by a stopping task. The copy
        # has a new MessageId but must still be able to finish the claim.
        self.make_job(total=1)
        message_id = self.enqueue(chunk_message())
        real_send = worker._send
        with mock.patch.object(worker, "RETRY_BACKOFF_SECONDS", 0), \
                mock.patch.object(worker, "_send", side_effect=RuntimeError("SQS down")):
            self.poll(FakeTranscriber())
        self.assertEqual(self.job()["synthesisEnqueued"], message_id)
        worker.process(self.receive(), FakeTranscriber(), FakeProtection(refuse=True))
        worker.stopping.clear()
        with mock.patch.object(worker, "_send", side_effect=real_send):
            self.poll(FakeTranscriber())
        self.assertEqual(self.drain("synthesis"), [{"jobId": JOB, "userId": USER}])
        self.assertEqual(self.counts(), (0, 0))

    def test_hand_back_falls_back_to_a_release_if_the_send_fails(self):
        self.make_job(total=1)
        message_id = self.enqueue(chunk_message())
        sqs = worker._client("sqs")
        with mock.patch.object(sqs, "send_message", side_effect=RuntimeError("AccessDenied")), \
                logs.capture() as records:
            self.protection = FakeProtection(refuse=True)
            self.poll(FakeTranscriber())
        self.assertEqual([r["msg"] for r in records],
                         ["could not hand back as a new message, released instead", "released, task is being stopped"])
        self.assertIsNone(records[-1]["newMessageId"])
        message = self.receive()
        self.assertEqual(message["MessageId"], message_id)  # the same message, visible at once
        self.assertEqual(message["Attributes"]["ApproximateReceiveCount"], "2")

    def test_protection_is_cleared_even_if_the_delete_fails(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        sqs = worker._client("sqs")
        with mock.patch.object(sqs, "delete_message", side_effect=RuntimeError("SQS down")):
            with self.assertRaises(RuntimeError):  # main() logs it and polls again; the message comes back
                self.poll(FakeTranscriber())
        self.assertEqual(self.protection.calls, [True, False])

    def test_heartbeat_extends_visibility_while_transcribing(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        with mock.patch.object(worker, "HEARTBEAT_SECONDS", 0.05), \
                mock.patch.object(worker, "release", wraps=worker.release) as release:
            self.poll(FakeTranscriber(seconds=0.3))
        extensions = [c for c in release.call_args_list if c.args[1] == worker.VISIBILITY_SECONDS]
        self.assertGreaterEqual(len(extensions), 2)
        self.assertEqual(self.counts(), (0, 0))  # and the delete still happened after the heartbeat stopped


class LogTest(Base):
    """the fields the trace query reads, and the metrics the dashboard reads."""

    def test_a_chunks_lines_name_the_job_and_the_transcription_is_timed(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        with logs.capture() as records:
            self.poll(FakeTranscriber())
        self.assertEqual([r["msg"] for r in records], ["transcribed", "chunk recorded", "synthesis requested"])
        for record in records:
            self.assertEqual(
                (record["service"], record["jobId"], record["userId"], record["chunk"], record["stage"]),
                ("whisper-worker", JOB, USER, 0, "Transcribing"),
            )
            self.assertIn("messageId", record)
        transcribed = records[0]
        self.assertEqual((transcribed["Step"], transcribed["StepDuration"]), ("transcribe", transcribed["durationMs"]))

    def test_a_failed_chunk_counts_an_error(self):
        self.make_job(total=1)
        self.enqueue(chunk_message())
        with logs.capture() as records:
            self.poll(FakeTranscriber(error=RuntimeError("model crashed")))
        [error] = records
        self.assertEqual((error["level"], error["Service"], error["Errors"], error["jobId"]),
                         ("ERROR", "whisper-worker", 1, JOB))

    def test_the_meter_logs_task_time_and_its_cost_outside_any_job(self):
        meter = worker.Meter()
        meter.last -= 30
        with logs.capture() as records, logs.scope(jobId=JOB):
            meter.emit()
        [line] = records
        self.assertNotIn("jobId", line)
        self.assertAlmostEqual(line["WorkerTaskSeconds"], 30, delta=1)
        self.assertAlmostEqual(line["FargateCost"], line["WorkerTaskSeconds"] * worker.FARGATE_PRICE_PER_HOUR / 3600)
        self.assertEqual([m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]],
                         ["WorkerTaskSeconds", "FargateCost"])


class ProtectionTest(unittest.TestCase):
    def response(self, body):
        return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(body).encode()), __exit__=lambda *a: False)

    def test_does_nothing_outside_a_task(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            worker.Protection().set(True)
        urlopen.assert_not_called()

    def test_enable_and_disable(self):
        protection = worker.Protection(agent_uri="http://169.254.170.2/api/abc")
        ok = {"protection": {"ProtectionEnabled": True, "TaskArn": "arn:x"}}
        with mock.patch("urllib.request.urlopen", return_value=self.response(ok)) as urlopen:
            protection.set(True)
            protection.set(False)
        first, second = (c.args[0] for c in urlopen.call_args_list)
        self.assertEqual((first.full_url, first.method), ("http://169.254.170.2/api/abc/task-protection/v1/state", "PUT"))
        self.assertEqual(json.loads(first.data), {"ProtectionEnabled": True, "ExpiresInMinutes": worker.PROTECTION_MINUTES})
        self.assertEqual(json.loads(second.data), {"ProtectionEnabled": False})
        self.assertTrue(protection.confirmed)

    def test_deployment_blocked_says_the_task_is_being_stopped(self):
        protection = worker.Protection(agent_uri="http://169.254.170.2/api/abc")
        blocked = {"failure": {"Arn": "arn:x", "Detail": None, "Reason": "DEPLOYMENT_BLOCKED"}}  # as logged
        with mock.patch("urllib.request.urlopen", return_value=self.response(blocked)):
            self.assertFalse(protection.set(True))
            self.assertTrue(protection.set(False))  # turning it off is never a reason to stop
        self.assertTrue(protection.available)

    def test_other_outcomes_carry_on(self):
        protection = worker.Protection(agent_uri="http://169.254.170.2/api/abc")
        ok = {"protection": {"ProtectionEnabled": True, "TaskArn": "arn:x"}}
        other = {"failure": {"Arn": "arn:x", "Detail": None, "Reason": "SOMETHING_ELSE"}}
        with mock.patch("urllib.request.urlopen", return_value=self.response(ok)):
            self.assertTrue(protection.set(True))
        with mock.patch("urllib.request.urlopen", return_value=self.response(other)):
            self.assertTrue(protection.set(True))
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            self.assertTrue(protection.set(True))
        self.assertTrue(worker.Protection().set(True))  # outside a task

    def test_access_denied_stops_trying(self):
        protection = worker.Protection(agent_uri="http://169.254.170.2/api/abc")
        denied = urllib.error.HTTPError(
            protection.url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":{"Arn":"arn:x","Code":"AccessDeniedException","Message":"not authorized"}}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=denied) as urlopen:
            protection.set(True)
            protection.set(False)
            protection.set(True)
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(protection.available)

    def test_agent_unreachable_is_tried_again(self):
        protection = worker.Protection(agent_uri="http://169.254.170.2/api/abc")
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()) as urlopen:
            protection.set(True)
            protection.set(True)
        self.assertEqual(urlopen.call_count, 2)
        self.assertTrue(protection.available)


if __name__ == "__main__":
    unittest.main()
