"""API service tests against moto (DynamoDB + S3). Run with `make test-api` (after `make venv-api`).

The caller is injected by overriding auth.current_user with the X-Test-User header, so these tests cover
routing, user scoping and the table logic. Token verification has its own tests in test_auth.py.
"""

import os

# Fake credentials before boto3 creates anything, so nothing can reach the real account.
os.environ.pop("AWS_PROFILE", None)
os.environ.update(
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_SESSION_TOKEN="testing",
    AWS_REGION="ap-southeast-2",
    AWS_DEFAULT_REGION="ap-southeast-2",
)

import contextlib
import decimal
import io
import re
import unittest
import unittest.mock
import urllib.parse

import boto3
import botocore.exceptions
from fastapi import Header
from fastapi.testclient import TestClient
from moto import mock_aws

from app import auth, keys, main, store
from contracts import logs
from app.routes import history

ALICE = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
BOB = "9b2e6f1a-0c3d-4e5f-8a7b-6c5d4e3f2a1b"
JOB = "01JA3M8Q2V7K9XG4T5N6P1R2S3"
REC = "01JA3M8P9W1X2Y3Z4A5B6C7D8E"
DOC = "01JA3MC4R5S6T7U8V9W0X1Y2Z3"


def test_user(x_test_user: str = Header()):
    return x_test_user


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        mock = mock_aws()
        mock.start()
        self.addCleanup(mock.stop)
        # The cache's EMF lines go to stdout; tests that read them redirect it again.
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        store.table.cache_clear()
        store.s3.cache_clear()
        self.addCleanup(store.table.cache_clear)
        self.addCleanup(store.s3.cache_clear)

        boto3.client("dynamodb", region_name="ap-southeast-2").create_table(
            TableName=store.TABLE_NAME,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            ],
        )
        boto3.client("s3", region_name="ap-southeast-2").create_bucket(
            Bucket=store.DOCS_BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )

        main.app.dependency_overrides[auth.current_user] = test_user
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)

    def get(self, path, user=ALICE, **kwargs):
        return self.client.get(path, headers={"X-Test-User": user}, **kwargs)

    def post(self, path, user=ALICE, **kwargs):
        return self.client.post(path, headers={"X-Test-User": user}, **kwargs)

    def create_project(self, user=ALICE, name="Pilot study"):
        response = self.post("/a3/projects", user, json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["projectId"]

    def put_job(self, project_id, user=ALICE, stage="Failed", **extra):
        store.table().put_item(
            Item={
                "PK": keys.job_pk(JOB),
                "SK": keys.JOB_SK,
                "GSI1PK": keys.project_pk(project_id),
                "GSI1SK": f"JOB#{JOB}",
                "userId": user,
                "recordingId": REC,
                "schemaId": "default",
                "stage": stage,
                **extra,
            }
        )

    def put_document(self, project_id, user=ALICE, title="Interview: P1 / onboarding"):
        store.table().put_item(
            Item={
                "PK": keys.project_pk(project_id),
                "SK": keys.doc_sk(DOC),
                "userId": user,
                "jobId": JOB,
                "recordingId": REC,
                "documentTitle": title,
                "storageKey": keys.document_object_key(user, project_id, DOC),
            }
        )
        store.s3().put_object(
            Bucket=store.DOCS_BUCKET, Key=keys.document_object_key(user, project_id, DOC), Body=b"# Findings\n"
        )


class Health(ApiTestCase):
    def test_healthz_needs_no_token(self):
        main.app.dependency_overrides.clear()
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})


class Logs(ApiTestCase):
    """M16: one line per request with the route, status, durationMs, the verified userId and the path's IDs."""

    def request_lines(self, records):
        return [r for r in records if r["msg"] == "request"]

    def test_request_line(self):
        project_id = self.create_project()
        self.put_job(project_id, stage="Failed", retryCount=0)
        with logs.capture() as records:
            self.post(f"/a3/jobs/{JOB}/retry")
        [line] = self.request_lines(records)
        self.assertEqual(
            {k: line[k] for k in ("service", "method", "route", "status", "userId", "jobId", "Route")},
            {"service": "api", "method": "POST", "route": "POST /a3/jobs/{job_id}/retry", "status": 202,
             "userId": ALICE, "jobId": JOB, "Route": "POST /a3/jobs/{job_id}/retry"},
        )
        self.assertEqual(line["RequestLatency"], line["durationMs"])

    def test_health_checks_are_not_logged_and_unauthenticated_requests_have_no_user(self):
        main.app.dependency_overrides.clear()
        with logs.capture() as records:
            self.client.get("/healthz")
            self.client.get(f"/a3/jobs/{JOB}")
        [line] = self.request_lines(records)
        self.assertEqual((line["status"], line["userId"], line["jobId"]), (401, None, JOB))

    def test_a_server_error_is_counted(self):
        client = TestClient(main.app, raise_server_exceptions=False)
        with logs.capture() as records, unittest.mock.patch.object(store, "list_projects", side_effect=RuntimeError("boom")):
            self.assertEqual(client.get("/a3/projects", headers={"X-Test-User": ALICE}).status_code, 500)
        line, error = [r for r in records if r["msg"] in ("request", "request failed")]
        self.assertEqual((line["level"], line["status"], line["route"]), ("ERROR", 500, "GET /a3/projects"))
        self.assertEqual((error["Service"], error["Errors"], error["userId"]), ("api", 1, ALICE))
        self.assertIn("boom", error["trace"])


class Unauthenticated(ApiTestCase):
    def test_every_a3_route_fails_closed(self):
        main.app.dependency_overrides.clear()
        # The OpenAPI document lists every route, including those on included routers.
        paths = {p: ops for p, ops in main.app.openapi()["paths"].items() if p.startswith("/a3")}
        self.assertEqual(len(paths), 9)
        for template, operations in paths.items():
            path = re.sub(r"\{\w+\}", "x", template)
            for method in operations:
                with self.subTest(method=method, path=template):
                    self.assertEqual(self.client.request(method, path).status_code, 401)


class Projects(ApiTestCase):
    def test_new_user_has_no_projects(self):
        response = self.get("/a3/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"projects": []})

    def test_create_then_list_and_get(self):
        project_id = self.create_project(name="  Pilot study  ")
        self.assertRegex(project_id, keys.ID_PATTERN)
        [listed] = self.get("/a3/projects").json()["projects"]
        self.assertEqual((listed["projectId"], listed["name"]), (project_id, "Pilot study"))

        store.table().put_item(
            Item={"PK": keys.project_pk(project_id), "SK": f"REC#{REC}", "fileName": "p1.mp4", "jobId": JOB}
        )
        self.put_document(project_id)
        body = self.get(f"/a3/projects/{project_id}").json()
        self.assertEqual(body["recordings"], [{"recordingId": REC, "fileName": "p1.mp4", "jobId": JOB}])
        self.assertEqual(body["documents"], [{"documentId": DOC, "title": "Interview: P1 / onboarding", "jobId": JOB}])

    def test_name_is_required(self):
        self.assertEqual(self.post("/a3/projects", json={"name": "   "}).status_code, 422)
        self.assertEqual(self.post("/a3/projects", json={}).status_code, 422)

    def test_id_with_key_delimiter_is_rejected(self):
        project_id = self.create_project()
        smuggled = urllib.parse.quote(f"{project_id}#x", safe="")
        self.assertEqual(self.get(f"/a3/projects/{smuggled}").status_code, 422)


class CrossUser(ApiTestCase):
    """User B gets 404, never 403 or data, for every one of user A's IDs."""

    def setUp(self):
        super().setUp()
        self.project_id = self.create_project(ALICE)
        self.put_job(self.project_id, ALICE, stage="Failed")
        self.put_document(self.project_id, ALICE)

    def test_bob_sees_none_of_alices_things(self):
        p = self.project_id
        self.assertEqual(self.get("/a3/projects", BOB).json(), {"projects": []})
        upload = {"recordings": [{"fileName": "p1.mp3", "sizeBytes": 1000}]}
        for method, path, body in [
            ("GET", f"/a3/projects/{p}", None),
            ("POST", f"/a3/projects/{p}/uploads", upload),
            ("GET", f"/a3/projects/{p}/documents/{DOC}", None),
            ("GET", f"/a3/projects/{p}/documents/{DOC}/export", None),
            ("GET", f"/a3/jobs/{JOB}", None),
            ("POST", f"/a3/jobs/{JOB}/retry", None),
            ("PATCH", f"/a3/projects/{p}", {"name": "Mine now"}),
            ("PATCH", f"/a3/projects/{p}/documents/{DOC}", {"title": "Mine now", "markdown": "# Mine\n"}),
            ("DELETE", f"/a3/projects/{p}/documents/{DOC}", None),
            ("DELETE", f"/a3/projects/{p}", None),
        ]:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers={"X-Test-User": BOB}, json=body)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"detail": "Not found"})

    def test_bobs_retry_leaves_alices_job_alone(self):
        self.post(f"/a3/jobs/{JOB}/retry", BOB)
        self.assertEqual(self.get(f"/a3/jobs/{JOB}").json()["stage"], "Failed")

    def test_bobs_changes_leave_alices_things_alone(self):
        p = self.project_id
        headers = {"X-Test-User": BOB}
        self.client.patch(f"/a3/projects/{p}", headers=headers, json={"name": "Mine now"})
        self.client.patch(f"/a3/projects/{p}/documents/{DOC}", headers=headers, json={"markdown": "# Mine\n"})
        self.client.delete(f"/a3/projects/{p}/documents/{DOC}", headers=headers)
        self.client.delete(f"/a3/projects/{p}", headers=headers)
        self.assertEqual(self.get(f"/a3/projects/{p}").json()["name"], "Pilot study")
        self.assertEqual(self.get(f"/a3/projects/{p}/documents/{DOC}").json()["markdown"], "# Findings\n")
        # Bob's PATCH didn't create a project of his own either (an update creates a missing item).
        self.assertEqual(self.get("/a3/projects", BOB).json(), {"projects": []})


class Jobs(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.project_id = self.create_project()

    def test_status(self):
        self.put_job(self.project_id, stage="Transcribing", chunksTotal=3, doneChunks={0, 2})
        self.assertEqual(
            self.get(f"/a3/jobs/{JOB}").json(),
            {
                "jobId": JOB,
                "projectId": self.project_id,
                "recordingId": REC,
                "schemaId": "default",
                "instructionText": None,
                "createdAt": None,
                "stage": "Transcribing",
                "audioSeconds": None,
                "chunksTotal": 3,
                "chunksDone": 2,
                "retryCount": 0,
                "failureReason": None,
                "documentId": None,
                "tokenCount": None,
                "cost": None,
            },
        )

    def test_done_job_points_at_its_document(self):
        self.put_job(
            self.project_id,
            stage="Done",
            instructionText="Focus on pricing",
            createdAt="2026-10-15T01:02:03.000Z",
            documentId=DOC,
            tokenCount=12480,
            cost=decimal.Decimal("0.0412"),
        )
        body = self.get(f"/a3/jobs/{JOB}").json()
        self.assertEqual(
            {k: body[k] for k in ("instructionText", "createdAt", "documentId", "tokenCount", "cost")},
            {
                "instructionText": "Focus on pricing",
                "createdAt": "2026-10-15T01:02:03.000Z",
                "documentId": DOC,
                "tokenCount": 12480,
                "cost": 0.0412,
            },
        )

    def test_missing_job(self):
        self.assertEqual(self.get(f"/a3/jobs/{JOB}").status_code, 404)
        self.assertEqual(self.post(f"/a3/jobs/{JOB}/retry").status_code, 404)
        self.assertNotIn("Item", store.table().get_item(Key={"PK": keys.job_pk(JOB), "SK": keys.JOB_SK}))

    def test_retry_failed_job(self):
        self.put_job(self.project_id, stage="Failed", retryCount=1, failureReason="Bedrock throttled")
        response = self.post(f"/a3/jobs/{JOB}/retry")
        self.assertEqual(response.status_code, 202)
        self.assertEqual((response.json()["stage"], response.json()["retryCount"]), ("Queued", 2))

    def test_retry_only_from_failed(self):
        self.put_job(self.project_id, stage="Queued")
        response = self.post(f"/a3/jobs/{JOB}/retry")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Queued", response.json()["detail"])


class Documents(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.project_id = self.create_project()
        self.put_document(self.project_id)

    def test_get(self):
        body = self.get(f"/a3/projects/{self.project_id}/documents/{DOC}").json()
        self.assertEqual(body["markdown"], "# Findings\n")
        self.assertEqual((body["title"], body["jobId"]), ("Interview: P1 / onboarding", JOB))
        self.assertEqual(body["contextFiles"], [])

    def test_lists_only_the_context_files_its_job_used(self):
        project_pk = keys.project_pk(self.project_id)
        for ctx, name in [("ctx-used", "guide.docx"), ("ctx-other", "notes.md")]:
            store.table().put_item(Item={"PK": project_pk, "SK": keys.ctx_sk(ctx), "fileName": name})
        store.table().put_item(Item={"PK": keys.job_pk(JOB), "SK": keys.ctx_sk("ctx-used")})
        body = self.get(f"/a3/projects/{self.project_id}/documents/{DOC}").json()
        self.assertEqual(body["contextFiles"], [{"contextFileId": "ctx-used", "fileName": "guide.docx"}])

    def test_document_without_owner_attribute_is_not_found(self):
        store.table().update_item(
            Key={"PK": keys.project_pk(self.project_id), "SK": keys.doc_sk(DOC)}, UpdateExpression="REMOVE userId"
        )
        self.assertEqual(self.get(f"/a3/projects/{self.project_id}/documents/{DOC}").status_code, 404)

    def test_export_is_a_presigned_attachment(self):
        body = self.get(f"/a3/projects/{self.project_id}/documents/{DOC}/export").json()
        self.assertEqual(body["fileName"], "Interview P1  onboarding.md")
        url = urllib.parse.urlparse(body["url"])
        query = urllib.parse.parse_qs(url.query)
        self.assertTrue(url.path.endswith(f"documents/{ALICE}/{self.project_id}/{DOC}.md"))
        self.assertEqual(query["response-content-disposition"], ['attachment; filename="Interview P1  onboarding.md"'])
        self.assertEqual(query["X-Amz-Expires"], ["300"])

    def test_export_file_name_cannot_break_the_header(self):
        # The title is model output: quotes, line breaks and non-ASCII must not reach Content-Disposition.
        for title, expected in [
            ('Q3 "pilot"\r\nX-Evil: 1 — résumé', "Q3 pilotX-Evil 1  rsum.md"),
            ("...hidden. ", "hidden.md"),
            ("???", "document.md"),
            (None, "document.md"),
        ]:
            with self.subTest(title=title):
                self.put_document(self.project_id, title=title)
                body = self.get(f"/a3/projects/{self.project_id}/documents/{DOC}/export").json()
                self.assertEqual(body["fileName"], expected)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(body["url"]).query)
                self.assertEqual(query["response-content-disposition"], [f'attachment; filename="{expected}"'])


class Changes(ApiTestCase):
    """M20: rename and delete a project, rename or edit a document, delete a document with its recording."""

    def setUp(self):
        super().setUp()
        self.project_id = self.create_project()
        store.table().put_item(Item={"PK": keys.project_pk(self.project_id), "SK": keys.rec_sk(REC), "jobId": JOB,
                                     "fileName": "p1.m4a"})
        self.put_job(self.project_id, stage="Done", documentId=DOC)
        self.put_document(self.project_id, title="Findings")
        self.doc = f"/a3/projects/{self.project_id}/documents/{DOC}"

    def patch(self, path, user=ALICE, **kwargs):
        return self.client.patch(path, headers={"X-Test-User": user}, **kwargs)

    def delete(self, path, user=ALICE):
        return self.client.delete(path, headers={"X-Test-User": user})

    def item(self, pk, sk):
        return store.table().get_item(Key={"PK": pk, "SK": sk}).get("Item")

    def test_rename_project(self):
        response = self.patch(f"/a3/projects/{self.project_id}", json={"name": "  Checkout redesign "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Checkout redesign")
        [listed] = self.get("/a3/projects").json()["projects"]
        self.assertEqual(listed["name"], "Checkout redesign")

    def test_rename_missing_project_creates_nothing(self):
        other = keys.new_id()
        self.assertEqual(self.patch(f"/a3/projects/{other}", json={"name": "Ghost"}).status_code, 404)
        self.assertIsNone(self.item(keys.user_pk(ALICE), keys.project_sk(other)))

    def test_delete_project(self):
        self.assertEqual(self.delete(f"/a3/projects/{self.project_id}").status_code, 204)
        self.assertEqual(self.get("/a3/projects").json(), {"projects": []})
        self.assertEqual(self.get(f"/a3/projects/{self.project_id}").status_code, 404)
        self.assertEqual(self.delete(f"/a3/projects/{self.project_id}").status_code, 404)
        # Only the Project item: the rest is GC's, through the stream (DeleteRequested).
        self.assertIsNotNone(self.item(keys.project_pk(self.project_id), keys.doc_sk(DOC)))
        self.assertIsNotNone(self.item(keys.job_pk(JOB), keys.JOB_SK))

    def test_uploads_into_a_deleted_project_are_refused(self):
        self.delete(f"/a3/projects/{self.project_id}")
        upload = {"recordings": [{"fileName": "p2.mp3", "sizeBytes": 1000}]}
        self.assertEqual(self.post(f"/a3/projects/{self.project_id}/uploads", json=upload).status_code, 404)

    def test_rename_document_rewrites_the_generated_heading(self):
        store.s3().put_object(Bucket=store.DOCS_BUCKET, Key=keys.document_object_key(ALICE, self.project_id, DOC),
                              Body=b"# Findings\n\n## Summary\n")
        response = self.patch(self.doc, json={"title": " Participant 3 "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Participant 3")
        body = self.get(self.doc).json()
        self.assertEqual((body["title"], body["markdown"]), ("Participant 3", "# Participant 3\n\n## Summary\n"))
        self.assertEqual(self.get(f"/a3/projects/{self.project_id}").json()["documents"][0]["title"], "Participant 3")

    def test_rename_leaves_an_edited_heading_alone(self):
        store.s3().put_object(Bucket=store.DOCS_BUCKET, Key=keys.document_object_key(ALICE, self.project_id, DOC),
                              Body=b"# My own heading\n")
        self.patch(self.doc, json={"title": "Participant 3"})
        self.assertEqual(self.get(self.doc).json()["markdown"], "# My own heading\n")

    def test_rename_still_works_if_the_heading_cant_be_written(self):
        store.s3().put_object(Bucket=store.DOCS_BUCKET, Key=keys.document_object_key(ALICE, self.project_id, DOC),
                              Body=b"# Findings\n")
        denied = botocore.exceptions.ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        with unittest.mock.patch.object(store, "_put_markdown", side_effect=denied):
            response = self.patch(self.doc, json={"title": "Participant 3"})
        self.assertEqual((response.status_code, response.json()["title"]), (200, "Participant 3"))
        self.assertEqual(self.get(self.doc).json()["markdown"], "# Findings\n")

    def test_edit_markdown(self):
        response = self.patch(self.doc, json={"markdown": "# Findings\n\nEdited by hand.\n"})
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.json()["updatedAt"], r"Z$")
        body = self.get(self.doc).json()
        self.assertEqual((body["title"], body["markdown"]), ("Findings", "# Findings\n\nEdited by hand.\n"))
        exported = store.s3().get_object(Bucket=store.DOCS_BUCKET,
                                         Key=keys.document_object_key(ALICE, self.project_id, DOC))
        self.assertEqual(exported["ContentType"], "text/markdown; charset=utf-8")

    def test_change_needs_something_valid(self):
        for body in [{}, {"title": "   "}, {"markdown": ""}, {"markdown": "x" * 500_001}, {"title": "t" * 201}]:
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.patch(self.doc, json=body).status_code, 422)

    def test_delete_document_takes_its_recording(self):
        self.assertEqual(self.delete(self.doc).status_code, 204)
        project = self.get(f"/a3/projects/{self.project_id}").json()
        self.assertEqual((project["recordings"], project["documents"]), ([], []))
        self.assertEqual(self.get(self.doc).status_code, 404)
        self.assertEqual(self.delete(self.doc).status_code, 404)
        # The job, the objects and the links are GC's (DeleteRequested names the job and the recording).
        self.assertIsNotNone(self.item(keys.job_pk(JOB), keys.JOB_SK))

    def test_delete_older_document_finds_its_recording_through_the_job(self):
        store.table().update_item(Key={"PK": keys.project_pk(self.project_id), "SK": keys.doc_sk(DOC)},
                                  UpdateExpression="REMOVE recordingId")
        self.assertEqual(self.delete(self.doc).status_code, 204)
        self.assertIsNone(self.item(keys.project_pk(self.project_id), keys.rec_sk(REC)))

    def test_patch_after_delete_does_not_recreate_it(self):
        self.delete(self.doc)
        self.assertEqual(self.patch(self.doc, json={"title": "Back"}).status_code, 404)
        self.assertIsNone(self.item(keys.project_pk(self.project_id), keys.doc_sk(DOC)))


class Schemas(ApiTestCase):
    def test_builtin_and_own_only(self):
        for pk, schema_id in [(keys.BUILTIN_SCHEMA_PK, "default"), (keys.user_pk(ALICE), "mine"), (keys.user_pk(BOB), "bobs")]:
            store.table().put_item(
                Item={"PK": pk, "SK": f"SCHEMA#{schema_id}", "name": schema_id.title(), "description": "d", "definition": "{}"}
            )
        schemas = self.get("/a3/schemas").json()["schemas"]
        self.assertEqual([(s["schemaId"], s["builtIn"]) for s in schemas], [("default", True), ("mine", False)])
        self.assertNotIn("definition", schemas[0])


class History(ApiTestCase):
    def setUp(self):
        super().setUp()
        for n in range(7):
            store.table().put_item(
                Item={
                    "PK": keys.user_pk(ALICE),
                    "SK": f"EVENT#2026-10-15T01:02:0{n}.000Z#evt{n}",
                    "eventType": "SynthesisDone",
                    "jobId": JOB,
                    "tokenCount": 1200 + n,
                    "cost": decimal.Decimal("0.0123"),
                }
            )

    def test_newest_first_with_cursor(self):
        first = self.get("/a3/history").json()
        self.assertEqual([e["eventId"] for e in first["events"]], ["evt6", "evt5", "evt4", "evt3", "evt2"])
        self.assertEqual(first["events"][0]["cost"], 0.0123)
        second = self.get("/a3/history", params={"cursor": first["nextCursor"], "limit": 10}).json()
        self.assertEqual([e["eventId"] for e in second["events"]], ["evt1", "evt0"])
        self.assertIsNone(second["nextCursor"])

    def test_job_context_and_failed_stage_are_returned(self):
        store.table().put_item(Item={
            "PK": keys.user_pk(ALICE), "SK": "EVENT#2026-10-15T01:03:00.000Z#failed", "eventType": "JobFailed",
            "jobId": JOB, "projectId": "p1", "fileName": "interview.m4a", "stage": "Transcribing",
        })
        [event] = self.get("/a3/history", params={"limit": 1}).json()["events"]
        self.assertEqual(
            event,
            {"eventId": "failed", "eventType": "JobFailed", "occurredAt": "2026-10-15T01:03:00.000Z", "jobId": JOB,
             "projectId": "p1", "fileName": "interview.m4a", "stage": "Transcribing"},
        )

    def test_the_history_writers_eventid_claims_are_not_events(self):
        # USER#<id> / EVENTID#<eventId> sorts after every EVENT#<timestamp> key, so it'd be first if it matched.
        store.table().put_item(Item={"PK": keys.user_pk(ALICE), "SK": "EVENTID#evt6", "eventSk": "EVENT#x"})
        first = self.get("/a3/history").json()
        self.assertEqual(first["events"][0]["eventId"], "evt6")

    def test_cursor_is_bound_to_the_caller(self):
        cursor = self.get("/a3/history").json()["nextCursor"]
        self.assertEqual(self.get("/a3/history", BOB, params={"cursor": cursor}).status_code, 400)
        self.assertEqual(self.get("/a3/history", params={"cursor": "not-a-cursor"}).status_code, 400)
        forged = history.encode_cursor({"PK": keys.user_pk(ALICE), "SK": "PROJECT#x"})
        self.assertEqual(self.get("/a3/history", params={"cursor": forged}).status_code, 400)

    def test_limit_bounds(self):
        self.assertEqual(self.get("/a3/history", params={"limit": 0}).status_code, 422)
        self.assertEqual(self.get("/a3/history", params={"limit": 51}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
