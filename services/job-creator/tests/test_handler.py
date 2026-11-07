"""Job creator against moto (S3, DynamoDB, SQS). ffmpeg is faked here (test_media.py covers it) except in
one end-to-end test. Events are captured and checked against the shared contract."""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
for _name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[_name] = "testing"
os.environ.pop("AWS_PROFILE", None)

import boto3
from moto import mock_aws

import handler
import media
from contracts import events, logs, messages

USER = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
OTHER = "9f8e7d6c-0000-4000-8000-000000000000"
PROJECT = "01a13e4f-e4a3-7292-9273-c616769610b9"
RECORDING = "01a13e4f-e79a-737d-8c36-3ac5dc0cc797"
JOB = "01a13e4f-e79a-737d-8c36-3ac634026915"
KEY = f"recordings/{USER}/{PROJECT}/{RECORDING}.m4a"
BUCKET = handler.UPLOADS_BUCKET


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
        """The entries in the shape a target receives, validated against the contract."""
        out = []
        for entry in self.entries:
            event = {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": json.loads(entry["Detail"])}
            events.validate_event(event)
            out.append(event)
        return out


def fake_split(n):
    """A media.split that writes n tiny chunk files."""

    def split(path, out_dir, chunk_seconds, timeout):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(n):
            (out_dir / f"{i}.wav").write_bytes(b"RIFF" + bytes([i]))
            paths.append(out_dir / f"{i}.wav")
        return paths

    return split


def s3_message(key=KEY, size=1234, event_name="ObjectCreated:Post", bucket=BUCKET):
    return {"Records": [{
        "eventSource": "aws:s3", "eventName": event_name,
        "s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}},
    }]}


def retry_event(retry_count=1):
    return {
        "version": "0", "source": events.SOURCE, "detail-type": "RetryRequested",
        "detail": {"eventId": "e1", "userId": USER, "occurredAt": "2026-10-15T01:02:03.456Z", "jobId": JOB,
                   "projectId": PROJECT, "recordingId": RECORDING, "retryCount": retry_count},
    }


def sqs_event(*bodies):
    return {"Records": [
        {"messageId": f"m{i}", "eventSource": "aws:sqs", "body": json.dumps(body)} for i, body in enumerate(bodies)
    ]}


class Context:
    aws_request_id = "8f0c1d2e-0000-4000-8000-000000000001"

    def get_remaining_time_in_millis(self):
        return 600_000


class Base(unittest.TestCase):
    """Moto resources plus the Recording and Queued Job the API pre-creates. No tests of its own."""

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
        self.s3 = boto3.client("s3", region_name="ap-southeast-2")
        self.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
        self.sqs = boto3.client("sqs", region_name="ap-southeast-2")
        self.queues = {}
        for name, attr in (("transcription", "TRANSCRIPTION_QUEUE_URL"), ("synthesis", "SYNTHESIS_QUEUE_URL")):
            url = self.sqs.create_queue(QueueName=f"interlixir-{name}")["QueueUrl"]
            self.queues[name] = url
            patcher = mock.patch.object(handler, attr, url)
            patcher.start()
            self.addCleanup(patcher.stop)

        # As the API's create_uploads writes them.
        self.table.put_item(Item={
            "PK": f"PROJECT#{PROJECT}", "SK": f"REC#{RECORDING}", "fileName": "interview.m4a",
            "storageKey": KEY, "languageCode": "en", "jobId": JOB, "createdAt": "2026-10-15T01:00:00.000Z",
        })
        self.table.put_item(Item={
            "PK": f"JOB#{JOB}", "SK": "META", "GSI1PK": f"PROJECT#{PROJECT}", "GSI1SK": f"JOB#{JOB}",
            "userId": USER, "recordingId": RECORDING, "schemaId": "default", "stage": "Queued", "retryCount": 0,
            "createdAt": "2026-10-15T01:00:00.000Z",
        })
        self.s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"audio")

    # --- helpers ---

    def job(self):
        return self.table.get_item(Key={"PK": f"JOB#{JOB}", "SK": "META"})["Item"]

    def set_job(self, **values):
        item = self.job()
        for name, value in values.items():
            if value is None:
                item.pop(name, None)
            else:
                item[name] = value
        self.table.put_item(Item=item)

    def drain(self, queue):
        bodies = []
        while True:
            got = self.sqs.receive_message(QueueUrl=self.queues[queue], MaxNumberOfMessages=10).get("Messages", [])
            if not got:
                return bodies
            for message in got:
                bodies.append(json.loads(message["Body"]))
                self.sqs.delete_message(QueueUrl=self.queues[queue], ReceiptHandle=message["ReceiptHandle"])

    def keys(self, prefix):
        return sorted(o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))

    def run_with(self, event, chunks=6, seconds=1800.0):
        with mock.patch.object(media, "probe", return_value=seconds), mock.patch.object(media, "split", fake_split(chunks)):
            return handler.handler(event, Context())


class JobCreatorTest(Base):
    # --- upload path ---

    def test_upload_chunks_and_queues(self):
        result = self.run_with(sqs_event(s3_message()))

        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(self.keys(f"chunks/{JOB}/"), [f"chunks/{JOB}/{n}.wav" for n in range(6)])
        bodies = self.drain("transcription")
        for body in bodies:
            messages.validate_message("TranscriptionChunk", body)
        self.assertEqual(sorted(b["chunk"] for b in bodies), list(range(6)))
        self.assertEqual({(b["chunksTotal"], b["userId"], b["jobId"]) for b in bodies}, {(6, USER, JOB)})
        self.assertEqual({b["chunk"]: b["startSeconds"] for b in bodies}, {n: n * 300 for n in range(6)})

        job = self.job()  # what the API shows as "Transcribing 0/6"
        self.assertEqual((job["stage"], job["chunksTotal"], job["audioSeconds"]), ("Transcribing", 6, 1800))
        self.assertNotIn("doneChunks", job)
        [event] = self.fake_events.delivered()
        self.assertEqual(event["detail-type"], "UploadReceived")
        self.assertEqual((event["detail"]["jobId"], event["detail"]["userId"]), (JOB, USER))

    def test_duplicate_s3_event_after_commit_does_nothing(self):
        self.run_with(sqs_event(s3_message()))
        self.drain("transcription")
        self.run_with(sqs_event(s3_message()))
        self.assertEqual(self.drain("transcription"), [])
        self.assertEqual(len(self.fake_events.entries), 1)

    def test_failure_before_commit_is_redone_on_redelivery(self):
        self.fake_events.fail_next = 1  # PutEvents fails after the chunk messages went out
        result = self.run_with(sqs_event(s3_message()))
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m0"}]})
        self.assertEqual(self.job()["stage"], "Queued")

        self.assertEqual(self.run_with(sqs_event(s3_message())), {"batchItemFailures": []})
        bodies = self.drain("transcription")
        self.assertEqual(len(bodies), 12)  # sent twice: harmless, G8 counts a set
        self.assertEqual(sorted({b["chunk"] for b in bodies}), list(range(6)))
        self.assertEqual(self.job()["stage"], "Transcribing")
        self.assertEqual(len(self.fake_events.entries), 1)

    def test_test_event_folder_and_empty_object_are_ignored(self):
        bodies = [
            {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": BUCKET},
            s3_message(key=f"recordings/{USER}/"),
            s3_message(size=0),
            s3_message(event_name="ObjectRemoved:Delete"),
            s3_message(bucket="someone-elses-bucket"),
        ]
        with mock.patch.object(handler, "chunk_and_enqueue") as work:
            self.assertEqual(handler.handler(sqs_event(*bodies), Context()), {"batchItemFailures": []})
        work.assert_not_called()

    def test_key_is_url_decoded(self):
        # A key with a space can't be a recording key, but it must be decoded before it's checked.
        with mock.patch.object(handler, "_get") as get:
            handler.handle_message(s3_message(key="recordings/a+b/c/d.wav"), Context())
            get.assert_not_called()
            handler.handle_message(s3_message(key=KEY.replace("-", "%2D")), Context())
            get.assert_called_once_with(f"PROJECT#{PROJECT}", f"REC#{RECORDING}")

    def test_keys_that_dont_match_a_recording_are_ignored(self):
        cases = [
            f"recordings/{USER}/{PROJECT}/{RECORDING}.exe",  # not an accepted extension
            f"recordings/{USER}/{PROJECT}/other.m4a",  # no Recording item (deleted, or not presigned)
            f"recordings/{OTHER}/{PROJECT}/{RECORDING}.m4a",  # storageKey belongs to someone else
            f"recordings/{USER}/{PROJECT}/{RECORDING}.mp3",  # storageKey has another extension
        ]
        with mock.patch.object(handler, "chunk_and_enqueue") as work:
            for key in cases:
                handler.handle_message(s3_message(key=key), Context())
        work.assert_not_called()

    def test_job_of_another_user_is_ignored(self):
        self.set_job(userId=OTHER)
        with mock.patch.object(handler, "chunk_and_enqueue") as work:
            handler.handle_message(s3_message(), Context())
        work.assert_not_called()

    def test_unreadable_file_fails_the_job(self):
        def bad(path, timeout):
            raise media.InputError("The file has no audio track.")

        with mock.patch.object(media, "probe", bad):
            self.assertEqual(handler.handler(sqs_event(s3_message()), Context()), {"batchItemFailures": []})
        job = self.job()
        self.assertEqual((job["stage"], job["failureReason"]), ("Failed", "The file has no audio track."))
        self.assertEqual(self.drain("transcription"), [])
        [event] = self.fake_events.delivered()
        self.assertEqual((event["detail-type"], event["detail"]["stage"]), ("JobFailed", "Queued"))
        self.assertEqual(event["detail"]["eventId"], f"jobfailed-{JOB}-0")

    def test_too_long_fails_the_job(self):
        self.run_with(sqs_event(s3_message()), seconds=3 * 3600 + 1)
        self.assertEqual(self.job()["stage"], "Failed")
        self.assertIn("longer than 3 hours", self.job()["failureReason"])
        self.assertEqual(self.keys("chunks/"), [])

    def test_too_long_when_duration_unknown(self):
        self.run_with(sqs_event(s3_message()), chunks=38, seconds=None)
        self.assertEqual(self.job()["stage"], "Failed")
        self.assertEqual(self.drain("transcription"), [])

    def test_one_bad_message_in_a_batch(self):
        with mock.patch.object(handler, "start", side_effect=[RuntimeError("boom"), None]):
            result = handler.handler(sqs_event(s3_message(), s3_message()), Context())
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m0"}]})

    # --- RetryRequested ---

    def failed_then_retried(self, **values):
        """The job as the API leaves it after POST /retry: Queued, retryCount + 1, old failureReason kept."""
        self.set_job(stage="Queued", retryCount=1, failureReason="Something broke.", **values)

    def put_transcripts(self, *numbers):
        for n in numbers:
            self.s3.put_object(Bucket=BUCKET, Key=f"{handler.transcripts_prefix(USER, JOB)}{n}.json", Body=b"{}")

    def put_chunks(self, total):
        for n in range(total):
            self.s3.put_object(Bucket=BUCKET, Key=handler.chunk_key(JOB, n), Body=b"RIFF")

    def test_retry_before_chunking_rechunks(self):
        self.failed_then_retried()
        self.run_with(sqs_event(retry_event()))
        self.assertEqual(len(self.drain("transcription")), 6)
        job = self.job()
        self.assertEqual((job["stage"], job["chunksTotal"]), ("Transcribing", 6))
        self.assertNotIn("failureReason", job)
        self.assertEqual(self.fake_events.entries, [])  # UploadReceived is only for the first upload

    def test_retry_reenqueues_only_missing_chunks(self):
        self.failed_then_retried(chunksTotal=6, doneChunks={0, 1, 2, 3}, synthesisEnqueued=True)
        self.put_chunks(6)
        self.put_transcripts(0, 1, 2)  # doneChunks said 3 was done, but its transcript isn't there
        self.run_with(sqs_event(retry_event()))

        bodies = self.drain("transcription")
        self.assertEqual(sorted(b["chunk"] for b in bodies), [3, 4, 5])
        job = self.job()
        self.assertEqual(job["doneChunks"], {0, 1, 2})
        self.assertNotIn("synthesisEnqueued", job)
        self.assertEqual(job["stage"], "Transcribing")
        self.assertEqual(self.drain("synthesis"), [])

    def test_retry_with_no_transcripts_removes_done_chunks(self):
        self.failed_then_retried(chunksTotal=2, doneChunks={0})
        self.put_chunks(2)
        self.run_with(sqs_event(retry_event()))
        self.assertEqual(len(self.drain("transcription")), 2)
        self.assertNotIn("doneChunks", self.job())

    def test_retry_with_every_transcript_goes_to_synthesis(self):
        self.failed_then_retried(chunksTotal=3, doneChunks={0, 1, 2}, synthesisEnqueued=True)
        self.put_chunks(3)
        self.put_transcripts(0, 1, 2)
        self.run_with(sqs_event(retry_event()))

        self.assertEqual(self.drain("transcription"), [])
        [body] = self.drain("synthesis")
        messages.validate_message("SynthesisRequest", body)
        self.assertEqual(body, {"jobId": JOB, "userId": USER})
        job = self.job()
        self.assertEqual((job["synthesisEnqueued"], job["doneChunks"]), (True, {0, 1, 2}))

    def test_retry_with_missing_chunk_audio_rechunks(self):
        self.failed_then_retried(chunksTotal=6)
        self.put_transcripts(0)
        self.run_with(sqs_event(retry_event()), chunks=6)
        self.assertEqual(len(self.drain("transcription")), 6)
        self.assertEqual(len(self.keys(f"chunks/{JOB}/")), 6)

    def test_retry_without_the_recording_fails(self):
        self.failed_then_retried()
        self.s3.delete_object(Bucket=BUCKET, Key=KEY)
        self.run_with(sqs_event(retry_event()))
        job = self.job()
        self.assertEqual(job["stage"], "Failed")
        self.assertIn("no longer stored", job["failureReason"])
        [event] = self.fake_events.delivered()
        self.assertEqual(event["detail"]["eventId"], f"jobfailed-{JOB}-1")

    def test_stale_or_duplicate_retry_is_ignored(self):
        self.failed_then_retried()
        with mock.patch.object(handler, "chunk_and_enqueue") as work:
            handler.handle_message(retry_event(retry_count=2), Context())  # not this retry
            self.set_job(stage="Transcribing")
            handler.handle_message(retry_event(retry_count=1), Context())  # already handled
        work.assert_not_called()

    def test_retry_invoked_directly_by_eventbridge(self):
        self.failed_then_retried()
        result = self.run_with(retry_event())
        self.assertEqual(result, {"resumed": JOB})
        self.assertEqual(self.job()["stage"], "Transcribing")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
class LogTest(Base):
    """M16: the fields the trace query reads, and the metrics the dashboard reads."""

    def test_chunking_is_timed_and_names_the_job(self):
        with logs.capture() as records:
            self.run_with(sqs_event(s3_message()))
        [chunked] = records
        self.assertEqual(
            (chunked["service"], chunked["msg"], chunked["jobId"], chunked["userId"], chunked["stage"]),
            ("job-creator", "chunked", JOB, USER, "Queued"),
        )
        self.assertEqual(chunked["requestId"], Context.aws_request_id)
        self.assertEqual((chunked["Step"], chunked["StepDuration"]), ("chunk", chunked["durationMs"]))

    def test_a_failed_job_and_an_error_are_counted(self):
        def bad(path, timeout):
            raise media.InputError("The file has no audio track.")

        with logs.capture() as records, mock.patch.object(media, "probe", bad):
            handler.handler(sqs_event(s3_message()), Context())
        [failed] = records
        self.assertEqual((failed["msg"], failed["JobsFailed"], failed["stage"]), ("failed", 1, "Queued"))

        self.set_job(stage="Queued")
        self.fake_events.fail_next = 1
        with logs.capture() as records:
            self.run_with(sqs_event(s3_message()))
        [error] = [r for r in records if r["level"] == "ERROR"]
        self.assertEqual((error["Service"], error["Errors"], error["jobId"]), ("job-creator", 1, JOB))


class EndToEndTest(Base):
    """The upload path with real ffmpeg: a 25 s stereo AAC file, 10 s chunks -> 3 x 16 kHz mono WAV."""

    def test_real_file(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        source = tmp / "a.m4a"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=duration=25",
                        "-ac", "2", "-c:a", "aac", str(source)], check=True)
        self.s3.put_object(Bucket=BUCKET, Key=KEY, Body=source.read_bytes())

        with mock.patch.object(handler, "CHUNK_SECONDS", 10):
            self.assertEqual(handler.handler(sqs_event(s3_message()), Context()), {"batchItemFailures": []})
        self.assertEqual(len(self.keys(f"chunks/{JOB}/")), 3)
        self.assertEqual(sorted(b["startSeconds"] for b in self.drain("transcription")), [0, 10, 20])
        job = self.job()
        self.assertEqual((job["stage"], job["chunksTotal"], job["audioSeconds"]), ("Transcribing", 3, 25))


if __name__ == "__main__":
    unittest.main()
