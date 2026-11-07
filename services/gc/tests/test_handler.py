"""GC against moto (DynamoDB + S3). Inputs are the shared contract's DeleteRequested examples, delivered the way the
SQS trigger delivers them. Run with `make test-gc` (one-time setup: `make venv-gc`).

The fixture is one project of ALICE's with two jobs from one upload (sharing a context file) and a third job from a
second upload with its own context file, every object the pipeline writes for them, and BOB's project next to it.
"""

import json
import os
import pathlib
import unittest

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
for _name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[_name] = "testing"
os.environ.pop("AWS_PROFILE", None)

import boto3
from moto import mock_aws

import handler
from contracts import events

EXAMPLES = pathlib.Path(events.__file__).with_name("examples")
ALICE = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"  # the examples' userId
BOB = "9b2e6f1a-0c3d-4e5f-8a7b-6c5d4e3f2a1b"
PROJECT = "01JA3M7Z0B6H8C2D4E5F6G7H8J"  # the examples' projectId
BOB_PROJECT = "01JA3M7Z0B6H8C2D4E5F6G7BOB"
# job -> (recordingId, context files it links to). Job 1 is the Document example's job.
JOBS = {
    "01JA3M8Q2V7K9XG4T5N6P1R2S3": ("01JA3M8P9W1X2Y3Z4A5B6C7D8E", ["ctx-shared"]),
    "01JA3M8Q2V7K9XG4T5N6P1JOB2": ("01JA3M8P9W1X2Y3Z4A5B6CREC2", ["ctx-shared"]),
    "01JA3M8Q2V7K9XG4T5N6P1JOB3": ("01JA3M8P9W1X2Y3Z4A5B6CREC3", ["ctx-own"]),
}
JOB1, JOB2, JOB3 = JOBS
BOB_JOB = "01JA3M8Q2V7K9XG4T5N6P1RBOB"


def example(name, **detail):
    event = json.loads((EXAMPLES / f"DeleteRequested.{name}.json").read_text())
    event["detail"].update(detail)
    event["detail"] = {k: v for k, v in event["detail"].items() if v is not None}
    return event


def delivered(*bodies):
    return {"Records": [
        {"messageId": f"m{i}", "eventSource": "aws:sqs",
         "eventSourceARN": "arn:aws:sqs:ap-southeast-2:123456789012:interlixir-gc",
         "body": body if isinstance(body, str) else json.dumps(body)}
        for i, body in enumerate(bodies)
    ]}


class GcTest(unittest.TestCase):
    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        handler._clients.clear()
        self.s3 = boto3.client("s3", region_name="ap-southeast-2")
        for bucket in (handler.UPLOADS_BUCKET, handler.DOCS_BUCKET):
            self.s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
        self.table = boto3.resource("dynamodb", region_name="ap-southeast-2").create_table(
            TableName=handler.TABLE_NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1", "Projection": {"ProjectionType": "KEYS_ONLY"},
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                              {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
            }],
        )
        for job_id, (recording_id, contexts) in JOBS.items():
            self.put_job(ALICE, PROJECT, job_id, recording_id, contexts)
        for context_id in ("ctx-shared", "ctx-own"):
            self.put_context(ALICE, PROJECT, context_id)
        self.put_project(ALICE, PROJECT)
        self.put_project(BOB, BOB_PROJECT)
        self.put_job(BOB, BOB_PROJECT, BOB_JOB, "01JA3M8P9W1X2Y3Z4A5B6CRBOB", [])

    # --- fixture -------------------------------------------------------------------------------------

    def put_object(self, bucket, key):
        self.s3.put_object(Bucket=bucket, Key=key, Body=b"x")

    def put_project(self, user, project_id):
        self.table.put_item(Item={"PK": f"USER#{user}", "SK": f"PROJECT#{project_id}", "projectName": "Pilot"})

    def put_job(self, user, project_id, job_id, recording_id, contexts):
        """A Done job: its items, its recording, chunks, transcripts, step files and document."""
        recording_key = f"recordings/{user}/{project_id}/{recording_id}.m4a"
        self.table.put_item(Item={"PK": f"JOB#{job_id}", "SK": "META", "GSI1PK": f"PROJECT#{project_id}",
                                  "GSI1SK": f"JOB#{job_id}", "userId": user, "recordingId": recording_id,
                                  "stage": "Done", "documentId": job_id})
        self.table.put_item(Item={"PK": f"PROJECT#{project_id}", "SK": f"REC#{recording_id}", "jobId": job_id,
                                  "storageKey": recording_key, "fileName": "p1.m4a"})
        self.table.put_item(Item={"PK": f"PROJECT#{project_id}", "SK": f"DOC#{job_id}", "userId": user,
                                  "jobId": job_id, "recordingId": recording_id, "documentTitle": "Notes",
                                  "storageKey": handler.document_key(user, project_id, job_id)})
        for context_id in contexts:
            self.table.put_item(Item={"PK": f"JOB#{job_id}", "SK": f"CTX#{context_id}"})
        self.put_object(handler.UPLOADS_BUCKET, recording_key)
        for n in (0, 1):
            self.put_object(handler.UPLOADS_BUCKET, f"chunks/{job_id}/{n}.wav")
            self.put_object(handler.UPLOADS_BUCKET, f"transcripts/{user}/{job_id}/chunks/{n}.json")
        for step in ("merged", "redacted", "document"):
            self.put_object(handler.UPLOADS_BUCKET, f"transcripts/{user}/{job_id}/{step}.json")
        self.put_object(handler.DOCS_BUCKET, handler.document_key(user, project_id, job_id))

    def put_context(self, user, project_id, context_id):
        key = f"context/{user}/{project_id}/{context_id}.pdf"
        self.table.put_item(Item={"PK": f"PROJECT#{project_id}", "SK": f"CTX#{context_id}", "storageKey": key})
        self.put_object(handler.UPLOADS_BUCKET, key)
        self.put_object(handler.UPLOADS_BUCKET, f"{key}.txt")

    # --- reading back --------------------------------------------------------------------------------

    def objects(self, bucket=None):
        buckets = [bucket] if bucket else [handler.UPLOADS_BUCKET, handler.DOCS_BUCKET]
        return {o["Key"] for b in buckets for o in self.s3.list_objects_v2(Bucket=b).get("Contents", [])}

    def items(self):
        return {(i["PK"], i["SK"]) for i in self.table.scan()["Items"]}

    def run_handler(self, *bodies):
        result = handler.handler(delivered(*bodies), None)
        self.assertEqual(result, {"batchItemFailures": []})

    def remove_document(self, job_id):
        """What the API's DELETE does: the Document and its Recording, in one transaction."""
        recording_id = JOBS[job_id][0]
        self.table.delete_item(Key={"PK": f"PROJECT#{PROJECT}", "SK": f"DOC#{job_id}"})
        self.table.delete_item(Key={"PK": f"PROJECT#{PROJECT}", "SK": f"REC#{recording_id}"})
        return example("document", documentId=job_id, jobId=job_id, recordingId=recording_id)

    def assert_nothing_of(self, user, project_id, job_ids):
        for key in self.objects():
            self.assertFalse(key.startswith((f"recordings/{user}/{project_id}/", f"context/{user}/{project_id}/",
                                             f"documents/{user}/{project_id}/")), key)
            for job_id in job_ids:
                self.assertFalse(key.startswith((f"chunks/{job_id}/", f"transcripts/{user}/{job_id}/")), key)
        for pk, sk in self.items():
            self.assertNotEqual(pk, f"PROJECT#{project_id}", sk)
            self.assertNotIn(pk, {f"JOB#{j}" for j in job_ids}, sk)

    # --- Project -------------------------------------------------------------------------------------

    def test_project_delete_leaves_no_orphans(self):
        bob_before = {k for k in self.objects() if BOB in k or BOB_JOB in k}
        self.table.delete_item(Key={"PK": f"USER#{ALICE}", "SK": f"PROJECT#{PROJECT}"})
        self.run_handler(example("project"))
        self.assert_nothing_of(ALICE, PROJECT, JOBS)
        # Bob's project, right next to it, is untouched.
        self.assertEqual({k for k in self.objects() if BOB in k or BOB_JOB in k}, bob_before)
        self.assertIn((f"JOB#{BOB_JOB}", "META"), self.items())
        self.assertIn((f"USER#{BOB}", f"PROJECT#{BOB_PROJECT}"), self.items())

    def test_project_delete_cascade_and_rerun_are_no_ops(self):
        self.table.delete_item(Key={"PK": f"USER#{ALICE}", "SK": f"PROJECT#{PROJECT}"})
        self.run_handler(example("project"))
        after = (self.objects(), self.items())
        # The removed Documents come back as DeleteRequested (one per job), and SQS may deliver the first again.
        documents = [example("document", documentId=j, jobId=j, recordingId=r) for j, (r, _) in JOBS.items()]
        self.run_handler(example("project"), *documents)
        self.assertEqual((self.objects(), self.items()), after)

    def test_project_that_still_exists_is_skipped(self):
        before = (self.objects(), self.items())
        self.run_handler(example("project"))
        self.assertEqual((self.objects(), self.items()), before)

    def test_another_users_job_in_the_project_index_is_left(self):
        # A job of Bob's that (somehow) names Alice's project: GC removes Alice's things only.
        self.table.update_item(Key={"PK": f"JOB#{BOB_JOB}", "SK": "META"},
                               UpdateExpression="SET GSI1PK = :pk", ExpressionAttributeValues={":pk": f"PROJECT#{PROJECT}"})
        self.table.delete_item(Key={"PK": f"USER#{ALICE}", "SK": f"PROJECT#{PROJECT}"})
        self.run_handler(example("project"))
        self.assertIn((f"JOB#{BOB_JOB}", "META"), self.items())
        self.assertIn(f"chunks/{BOB_JOB}/0.wav", self.objects())

    # --- Document ------------------------------------------------------------------------------------

    def test_document_delete_removes_its_recording_and_job(self):
        self.run_handler(self.remove_document(JOB1))
        objects, items = self.objects(), self.items()
        recording_id = JOBS[JOB1][0]
        self.assertNotIn(handler.document_key(ALICE, PROJECT, JOB1), objects)
        self.assertNotIn(f"recordings/{ALICE}/{PROJECT}/{recording_id}.m4a", objects)
        self.assertFalse([k for k in objects if k.startswith((f"chunks/{JOB1}/", f"transcripts/{ALICE}/{JOB1}/"))])
        self.assertFalse([i for i in items if i[0] == f"JOB#{JOB1}"])
        # The context file is still used by job 2 (same upload): it stays, with its extracted text.
        self.assertIn((f"PROJECT#{PROJECT}", "CTX#ctx-shared"), items)
        self.assertIn(f"context/{ALICE}/{PROJECT}/ctx-shared.pdf.txt", objects)
        # The other jobs are untouched.
        for job_id in (JOB2, JOB3):
            self.assertIn((f"JOB#{job_id}", "META"), items)
            self.assertIn(handler.document_key(ALICE, PROJECT, job_id), objects)
            self.assertIn(f"chunks/{job_id}/0.wav", objects)

    def test_last_job_using_a_context_file_takes_it(self):
        self.run_handler(self.remove_document(JOB1), self.remove_document(JOB2))
        self.assertNotIn((f"PROJECT#{PROJECT}", "CTX#ctx-shared"), self.items())
        self.assertFalse([k for k in self.objects() if "ctx-shared" in k])
        self.run_handler(self.remove_document(JOB3))
        self.assertFalse([k for k in self.objects() if "/ctx-" in k])
        # Only the (empty) project is left.
        self.assertEqual({i for i in self.items() if i[0] in (f"USER#{ALICE}", f"PROJECT#{PROJECT}")},
                         {(f"USER#{ALICE}", f"PROJECT#{PROJECT}")})

    def test_document_delete_twice_is_a_no_op(self):
        event = self.remove_document(JOB1)
        self.run_handler(event)
        after = (self.objects(), self.items())
        self.run_handler(event)
        self.assertEqual((self.objects(), self.items()), after)

    def test_document_without_job_or_recording_ids(self):
        # An older Document: jobId is the documentId, recordingId comes from the job.
        self.remove_document(JOB1)
        self.run_handler(example("document", documentId=JOB1, jobId=None, recordingId=None))
        self.assertNotIn(f"recordings/{ALICE}/{PROJECT}/{JOBS[JOB1][0]}.m4a", self.objects())
        self.assertNotIn((f"JOB#{JOB1}", "META"), self.items())

    def test_document_that_still_exists_is_skipped(self):
        before = (self.objects(), self.items())
        self.run_handler(example("document", documentId=JOB1, jobId=JOB1))
        self.assertEqual((self.objects(), self.items()), before)

    def test_document_naming_another_users_job_leaves_the_job(self):
        before = {k for k in self.objects() if BOB_JOB in k}
        self.run_handler(example("document", documentId="01JA3MC4R5S6T7U8V9W0X1Y2Z3", jobId=BOB_JOB, recordingId=None))
        self.assertIn((f"JOB#{BOB_JOB}", "META"), self.items())
        self.assertEqual({k for k in self.objects() if BOB_JOB in k}, before)

    # --- input ---------------------------------------------------------------------------------------

    def test_invalid_events_are_dropped_not_retried(self):
        before = (self.objects(), self.items())
        bad = [
            "not json",
            json.dumps({"source": "aws.s3", "detail-type": "DeleteRequested", "detail": {}}),
            example("project", userId="../other"),
            example("project", projectId="a/b"),
            example("project", documentId=JOB1),
            example("document", documentId=None),
            example("document", recordingId="x#y"),
            example("document", entity="Recording"),
        ]
        self.run_handler(*bad)
        self.assertEqual((self.objects(), self.items()), before)

    def test_an_aws_error_fails_only_that_record(self):
        self.table.delete_item(Key={"PK": f"USER#{ALICE}", "SK": f"PROJECT#{PROJECT}"})
        handler._clients["s3"] = Broken()
        result = handler.handler(delivered(example("project"), "not json"), None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m0"}]})

    def test_direct_eventbridge_target(self):
        self.table.delete_item(Key={"PK": f"USER#{ALICE}", "SK": f"PROJECT#{PROJECT}"})
        self.assertIsNone(handler.handler(example("project"), None))
        self.assert_nothing_of(ALICE, PROJECT, JOBS)


class Broken:
    def __getattr__(self, name):
        def fail(**_):
            raise RuntimeError("S3 is down")
        return fail


if __name__ == "__main__":
    unittest.main()
