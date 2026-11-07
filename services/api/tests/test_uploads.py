"""Presign route tests against moto. The policies are decoded and checked condition by condition: S3
enforces them, so what they say is what an upload can do. The live check that S3 really rejects an
oversized file is scripts/check_uploads.py.
"""

import base64
import datetime
import json
from unittest import mock

from boto3.dynamodb.conditions import Key

from app import keys, store
from contracts import logs
from app.routes import uploads

from .test_api import ALICE, BOB, ApiTestCase

MiB = 1024 * 1024


def policy_of(upload):
    return json.loads(base64.b64decode(upload["fields"]["policy"]))


class UploadTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        store.table().put_item(Item={"PK": keys.BUILTIN_SCHEMA_PK, "SK": "SCHEMA#default", "name": "Default"})
        self.project_id = self.create_project()

    def upload(self, body, user=ALICE, project_id=None):
        return self.post(f"/a3/projects/{project_id or self.project_id}/uploads", user, json=body)

    def items(self, pk):
        return store.table().query(KeyConditionExpression=Key("PK").eq(pk))["Items"]

    def assert_nothing_written(self):
        """Only the project itself: no recordings, context files or jobs."""
        self.assertEqual(self.items(keys.project_pk(self.project_id)), [])
        self.assertEqual(store.table().scan(ScanFilter={"SK": {"AttributeValueList": ["META"], "ComparisonOperator": "EQ"}})["Items"], [])


class Created(UploadTestCase):
    def setUp(self):
        super().setUp()
        self.records = self.enterContext(logs.capture())
        self.response = self.upload(
            {
                "schemaId": "default",
                "instructionText": "  Focus on onboarding pain points.  ",
                "recordings": [
                    {"fileName": "P1 interview.MP3", "contentType": "audio/mp3", "sizeBytes": 150 * MiB},
                    {"fileName": "p2.m4a", "contentType": "audio/x-m4a", "sizeBytes": 1234},
                ],
                "contextFiles": [{"fileName": "guide.md", "contentType": "", "sizeBytes": 2048}],
            }
        )
        self.assertEqual(self.response.status_code, 201, self.response.text)
        self.body = self.response.json()

    def test_each_job_is_logged_where_its_trace_starts(self):
        created = [r for r in self.records if r["msg"] == "job created"]
        self.assertEqual([(r["jobId"], r["userId"], r["stage"]) for r in created],
                         [(rec["jobId"], ALICE, "Queued") for rec in self.body["recordings"]])

    def test_items_are_pre_created(self):
        [ctx] = self.body["contextFiles"]
        project_items = {i["SK"]: i for i in self.items(keys.project_pk(self.project_id))}
        self.assertEqual(len(project_items), 3)
        context_item = project_items[keys.ctx_sk(ctx["contextFileId"])]
        self.assertEqual(context_item["fileName"], "guide.md")
        self.assertEqual(
            context_item["storageKey"], f"context/{ALICE}/{self.project_id}/{ctx['contextFileId']}.md"
        )

        for rec, ext in zip(self.body["recordings"], ["mp3", "m4a"]):
            recording = project_items[keys.rec_sk(rec["recordingId"])]
            self.assertEqual(recording["jobId"], rec["jobId"])
            self.assertEqual(recording["languageCode"], "en")
            self.assertEqual(
                recording["storageKey"], f"recordings/{ALICE}/{self.project_id}/{rec['recordingId']}.{ext}"
            )
            job_items = {i["SK"]: i for i in self.items(keys.job_pk(rec["jobId"]))}
            self.assertEqual(set(job_items), {"META", keys.ctx_sk(ctx["contextFileId"])})
            job = job_items["META"]
            self.assertEqual(
                {k: job[k] for k in ("stage", "userId", "recordingId", "schemaId", "instructionText", "retryCount")},
                {
                    "stage": "Queued",
                    "userId": ALICE,
                    "recordingId": rec["recordingId"],
                    "schemaId": "default",
                    "instructionText": "Focus on onboarding pain points.",
                    "retryCount": 0,
                },
            )
            self.assertEqual((job["GSI1PK"], job["GSI1SK"]), (keys.project_pk(self.project_id), f"JOB#{rec['jobId']}"))
            self.assertNotIn("chunksTotal", job)  # the job creator's dedupe condition

    def test_jobs_show_queued(self):
        for rec in self.body["recordings"]:
            job = self.get(f"/a3/jobs/{rec['jobId']}").json()
            self.assertEqual((job["stage"], job["projectId"], job["chunksDone"]), ("Queued", self.project_id, 0))

    def test_project_lists_the_recordings(self):
        recordings = self.get(f"/a3/projects/{self.project_id}").json()["recordings"]
        self.assertEqual({r["fileName"] for r in recordings}, {"P1 interview.MP3", "p2.m4a"})

    def test_policies(self):
        expected = [
            (self.body["recordings"][0], "audio/mpeg", 150 * MiB, "mp3"),
            (self.body["recordings"][1], "audio/mp4", 1234, "m4a"),
            (self.body["contextFiles"][0], "text/markdown", 2048, "md"),
        ]
        for created, content_type, size, ext in expected:
            with self.subTest(file=created["fileName"]):
                upload = created["upload"]
                self.assertEqual(upload["url"], f"https://{store.UPLOADS_BUCKET}.s3.ap-southeast-2.amazonaws.com/")
                policy = policy_of(upload)
                conditions = policy["conditions"]
                self.assertIn(["content-length-range", 1, size], conditions)
                self.assertIn({"Content-Type": content_type}, conditions)
                self.assertIn({"bucket": store.UPLOADS_BUCKET}, conditions)
                key = upload["fields"]["key"]
                self.assertIn({"key": key}, conditions)
                self.assertTrue(key.startswith(f"{'recordings' if ext != 'md' else 'context'}/{ALICE}/{self.project_id}/"))
                self.assertTrue(key.endswith(f".{ext}"))
                self.assertEqual(upload["fields"]["Content-Type"], content_type)
                expires = datetime.datetime.fromisoformat(policy["expiration"])
                remaining = expires - datetime.datetime.now(datetime.timezone.utc)
                self.assertTrue(datetime.timedelta(minutes=14) < remaining <= datetime.timedelta(minutes=15))
        self.assertEqual(self.body["expiresIn"], 900)


class Rejected(UploadTestCase):
    def test_invalid_requests_write_nothing(self):
        rec = {"fileName": "p1.wav", "sizeBytes": 1000}
        cases = {
            "no recordings": {"recordings": []},
            "six recordings": {"recordings": [rec] * 6},
            "bad extension": {"recordings": [{"fileName": "p1.mov", "sizeBytes": 10}]},
            "no extension": {"recordings": [{"fileName": "p1", "sizeBytes": 10}]},
            "type doesn't match extension": {"recordings": [{**rec, "contentType": "video/mp4"}]},
            "recording over 200 MiB": {"recordings": [{**rec, "sizeBytes": 200 * MiB + 1}]},
            "empty recording": {"recordings": [{**rec, "sizeBytes": 0}]},
            "path in file name": {"recordings": [{**rec, "fileName": "../p1.wav"}]},
            "context image": {"recordings": [rec], "contextFiles": [{"fileName": "a.png", "sizeBytes": 10}]},
            "six context files": {"recordings": [rec], "contextFiles": [{"fileName": "a.txt", "sizeBytes": 10}] * 6},
            "context total over 20 MiB": {
                "recordings": [rec],
                "contextFiles": [{"fileName": "a.pdf", "sizeBytes": 10 * MiB}, {"fileName": "b.pdf", "sizeBytes": 10 * MiB + 1}],
            },
            "instruction too long": {"recordings": [rec], "instructionText": "x" * 4001},
            "schema id with a delimiter": {"recordings": [rec], "schemaId": "SCHEMA#x"},
            "unknown schema": {"recordings": [rec], "schemaId": "nope"},
            "someone else's schema": {"recordings": [rec], "schemaId": "bobs"},
        }
        store.table().put_item(Item={"PK": keys.user_pk(BOB), "SK": "SCHEMA#bobs", "name": "Bob's"})
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(self.upload(body).status_code, 422)
        self.assert_nothing_written()

    def test_limits_are_inclusive(self):
        body = {
            "recordings": [{"fileName": f"p{n}.webm", "contentType": "video/webm", "sizeBytes": 200 * MiB} for n in range(5)],
            "contextFiles": [{"fileName": "a.docx", "sizeBytes": 10 * MiB}, {"fileName": "b.txt", "sizeBytes": 10 * MiB}],
            "instructionText": "x" * 4000,
        }
        self.assertEqual(self.upload(body).status_code, 201)

    def test_own_schema_and_unknown_content_type_are_accepted(self):
        store.table().put_item(Item={"PK": keys.user_pk(ALICE), "SK": "SCHEMA#mine", "name": "Mine"})
        body = {"schemaId": "mine", "recordings": [{"fileName": "p1.mp4", "contentType": "application/octet-stream", "sizeBytes": 5}]}
        response = self.upload(body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["recordings"][0]["upload"]["fields"]["Content-Type"], "video/mp4")

    def test_other_users_project_is_404_and_writes_nothing(self):
        response = self.upload({"recordings": [{"fileName": "p1.mp3", "sizeBytes": 10}]}, user=BOB)
        self.assertEqual(response.status_code, 404)
        self.assert_nothing_written()

    def test_project_deleted_mid_request_writes_nothing(self):
        # The route's own lookup passed; the project goes before the transaction runs.
        with mock.patch.object(store, "get_project"):
            store.table().delete_item(Key={"PK": keys.user_pk(ALICE), "SK": keys.project_sk(self.project_id)})
            response = self.upload({"recordings": [{"fileName": "p1.mp3", "sizeBytes": 10}], "contextFiles": [{"fileName": "a.txt", "sizeBytes": 1}]})
        self.assertEqual(response.status_code, 404)
        self.assert_nothing_written()

    def test_type_table_is_consistent(self):
        for types in (uploads.RECORDING_TYPES, uploads.CONTEXT_TYPES):
            for ext, (canonical, accepted) in types.items():
                with self.subTest(ext=ext):
                    self.assertIn(canonical, accepted)
