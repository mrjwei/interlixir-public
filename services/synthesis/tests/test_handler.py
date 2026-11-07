"""Synthesis handler against moto (S3, DynamoDB, SQS). Bedrock is faked: it "finds" the planted PII in whatever
text it's given, returns a canned document for the default template and a title. Events are captured and checked
against the shared contract; the stored document is validated against the template."""

import copy
import json
import os
import pathlib
import unittest
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
for _name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[_name] = "testing"
os.environ.pop("AWS_PROFILE", None)
os.environ["MODEL_PRICES_PARAMETER"] = ""  # no Parameter Store in tests: prices come from MODEL_PRICES
os.environ["MODEL_PRICES"] = json.dumps({
    "nvidia.nemotron-super-3-120b": {"input": 1.0, "output": 2.0},
    "google.gemma-3-12b-it": {"input": 0.5, "output": 0.5},
})

import boto3
from jsonschema import Draft202012Validator
from moto import mock_aws

import bedrock
import handler
from contracts import events, logs, messages, transcripts

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_SCHEMA_TEXT = (ROOT / "schemas" / "builtin" / "default.json").read_text()
DEFAULT_SCHEMA = json.loads(DEFAULT_SCHEMA_TEXT)

USER = "3c1d5a2e-7f40-4b8e-9a61-2d9e8b0f4c17"
PROJECT = "01a13e4f-e4a3-7292-9273-c616769610b9"
RECORDING = "01a13e4f-e79a-737d-8c36-3ac5dc0cc797"
JOB = "01a13e4f-e79a-737d-8c36-3ac634026915"
CTX = "01a13e4f-e79a-737d-8c36-3ac6aaaaaaaa"
CTX_KEY = f"context/{USER}/{PROJECT}/{CTX}.txt"

CHUNKS = [
    [
        {"start": 1.0, "end": 5.0, "text": "Thanks for joining. Could you introduce yourself?"},
        {"start": 6.0, "end": 12.0, "text": "Sure, I'm Sarah Chen and I'm a nurse at Brightwater Clinic."},
    ],
    [
        {"start": 301.0, "end": 306.0, "text": "Changing my delivery address was confusing, the search bar was hidden."},
        {"start": 307.0, "end": 312.0, "text": "If you need more, Sarah is on 0412 345 678 or sarah.chen@example.com."},
    ],
]
PLANTED = ("Sarah", "Chen", "Brightwater", "0412 345 678", "sarah.chen@example.com")

PII = [
    {"text": "Sarah Chen", "type": "person", "refersTo": "Sarah Chen"},
    {"text": "Sarah", "type": "person", "refersTo": "Sarah"},
    {"text": "Brightwater Clinic", "type": "organisation", "refersTo": "Brightwater Clinic"},
]

DOCUMENT = {
    "summary": "A nurse described how hard it was to change a delivery address.",
    "participants": [{"pseudonym": "Participant 1", "role": "Nurse"}, {"pseudonym": "Interviewer", "role": "Researcher"}],
    "themes": [{
        "theme": "Hidden search",
        "description": "The search bar was hard to find while changing an address.",
        "evidence": [{"speaker": "Participant 1", "text": "the search bar was hidden", "timestamp": "05:01"}],
    }],
    "painPoints": ["Changing the delivery address"],
    "opportunities": ["Make search visible on the address page"],
    "notableQuotes": [{"speaker": "Participant 1", "text": "Changing my delivery address was confusing"}],
    "openQuestions": [],
}


class FakeBedrock:
    """Stands in for bedrock-runtime. Records every converse call."""

    def __init__(self):
        self.calls = []
        self.documents = [DOCUMENT]  # write_document replies in order; the last one repeats
        self.title = "Hidden search frustrates address changes"
        self.title_error = None

    def converse(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        tools = kwargs.get("toolConfig", {}).get("tools", [])
        name = tools[0]["toolSpec"]["name"] if tools else None
        if name == "report_pii":
            text = kwargs["messages"][0]["content"][0]["text"]
            return self._tool(name, {"entities": [e for e in PII if e["text"] in text]})
        if name == "write_document":
            reply = self.documents.pop(0) if len(self.documents) > 1 else self.documents[0]
            return self._tool(name, reply)
        if self.title_error:
            raise self.title_error
        return {"output": {"message": {"role": "assistant", "content": [{"text": self.title}]}},
                "stopReason": "end_turn", "usage": {"inputTokens": 50, "outputTokens": 8}}

    def tools_called(self, name=None):
        found = []
        for call in self.calls:
            tools = call.get("toolConfig", {}).get("tools", [])
            found.append(tools[0]["toolSpec"]["name"] if tools else "title")
        return [n for n in found if name is None or n == name]

    @staticmethod
    def _tool(name, value):
        return {
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": f"t-{name}", "name": name, "input": copy.deepcopy(value)}}
            ]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 1000, "outputTokens": 100},
        }


class FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "x"} for _ in Entries]}

    def delivered(self):
        out = []
        for entry in self.entries:
            event = {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": json.loads(entry["Detail"])}
            events.validate_event(event)
            out.append(event)
        return out


class Context:
    def __init__(self, seconds=900):
        self.seconds = seconds

    def get_remaining_time_in_millis(self):
        return self.seconds * 1000


class Base(unittest.TestCase):
    """A project, its recording, a job at "Transcribing 2/2" with the synthesis claim, two chunk transcripts,
    the default template and one context file, as M5-M7 leave them."""

    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        handler._clients.clear()
        self.bedrock = FakeBedrock()
        self.events = FakeEvents()
        handler._clients["bedrock-runtime"] = self.bedrock
        handler._clients["events"] = self.events

        ddb = boto3.resource("dynamodb", region_name="ap-southeast-2")
        self.table = ddb.create_table(
            TableName=handler.TABLE_NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        self.s3 = boto3.client("s3", region_name="ap-southeast-2")
        for bucket in (handler.UPLOADS_BUCKET, handler.DOCS_BUCKET):
            self.s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
        self.sqs = boto3.client("sqs", region_name="ap-southeast-2")
        self.queue = self.sqs.create_queue(QueueName="interlixir-synthesis")["QueueUrl"]
        handler.SYNTHESIS_QUEUE_URL = self.queue

        put = self.table.put_item
        put(Item={"PK": f"USER#{USER}", "SK": f"PROJECT#{PROJECT}", "projectName": "Study"})
        put(Item={"PK": f"PROJECT#{PROJECT}", "SK": f"REC#{RECORDING}", "fileName": "interview-1.m4a",
                  "storageKey": f"recordings/{USER}/{PROJECT}/{RECORDING}.m4a", "jobId": JOB})
        put(Item={"PK": f"JOB#{JOB}", "SK": "META", "GSI1PK": f"PROJECT#{PROJECT}", "GSI1SK": f"JOB#{JOB}",
                  "userId": USER, "recordingId": RECORDING, "schemaId": "default", "stage": "Transcribing",
                  "retryCount": 0, "chunksTotal": 2, "doneChunks": {0, 1}, "synthesisEnqueued": "m-1",
                  "instructionText": "Focus on the checkout flow."})
        put(Item={"PK": "SCHEMA#BUILTIN", "SK": "SCHEMA#default", "name": DEFAULT_SCHEMA["title"],
                  "description": DEFAULT_SCHEMA["description"], "definition": DEFAULT_SCHEMA_TEXT})
        put(Item={"PK": f"PROJECT#{PROJECT}", "SK": f"CTX#{CTX}", "fileName": "guide.txt", "storageKey": CTX_KEY})
        put(Item={"PK": f"JOB#{JOB}", "SK": f"CTX#{CTX}"})
        self.s3.put_object(Bucket=handler.UPLOADS_BUCKET, Key=CTX_KEY, Body=b"\xef\xbb\xbfDiscussion guide: ask about checkout.")
        for n, segments in enumerate(CHUNKS):
            self.put_transcript(n, segments)

    def put_transcript(self, n, segments):
        body = {"jobId": JOB, "chunk": n, "startSeconds": n * 300, "model": "distil-small.en", "language": "en",
                "audioSeconds": 300.0, "text": " ".join(s["text"] for s in segments), "segments": segments}
        transcripts.validate_transcript(body)
        self.s3.put_object(Bucket=handler.UPLOADS_BUCKET, Key=handler.chunk_transcript_key(USER, JOB, n),
                           Body=json.dumps(body).encode())

    def run_handler(self, body=None, seconds=900, receives=1):
        if body is None:
            body = {"jobId": JOB, "userId": USER}
            messages.validate_message("SynthesisRequest", body)
        self.sqs.send_message(QueueUrl=self.queue, MessageBody=json.dumps(body))
        message = self.sqs.receive_message(QueueUrl=self.queue, VisibilityTimeout=600)["Messages"][0]
        record = {"messageId": message["MessageId"], "receiptHandle": message["ReceiptHandle"],
                  "body": message["Body"], "attributes": {"ApproximateReceiveCount": str(receives)}}
        return handler.handler({"Records": [record]}, Context(seconds))

    def job(self):
        return self.table.get_item(Key={"PK": f"JOB#{JOB}", "SK": "META"})["Item"]

    def document_item(self):
        return self.table.get_item(Key={"PK": f"PROJECT#{PROJECT}", "SK": f"DOC#{JOB}"}).get("Item")

    def markdown(self):
        key = handler.document_key(USER, PROJECT, JOB)
        return self.s3.get_object(Bucket=handler.DOCS_BUCKET, Key=key)["Body"].read().decode()

    def saved(self, step):
        obj = self.s3.get_object(Bucket=handler.UPLOADS_BUCKET, Key=handler.step_key(USER, JOB, step))
        return json.loads(obj["Body"].read())

    def project_item(self):
        return self.table.get_item(Key={"PK": f"USER#{USER}", "SK": f"PROJECT#{PROJECT}"}).get("Item")

    def exists(self, bucket, key):
        return "Contents" in self.s3.list_objects_v2(Bucket=bucket, Prefix=key)

    def visible_now(self):
        return len(self.sqs.receive_message(QueueUrl=self.queue, WaitTimeSeconds=0).get("Messages", []))


class TestHappyPath(Base):
    def test_default_template_produces_a_valid_document(self):
        self.assertEqual(self.run_handler(), {"batchItemFailures": []})

        job = self.job()
        self.assertEqual(job["stage"], "Done")
        self.assertEqual(job["documentId"], JOB)
        # One redaction window (a short transcript), the synthesis call, the title.
        self.assertEqual(self.bedrock.tools_called(), ["report_pii", "write_document", "title"])
        self.assertEqual(int(job["tokenCount"]), 1100 + 1100 + 58)
        self.assertAlmostEqual(float(job["cost"]), (2000 * 1.0 + 200 * 2.0 + 50 * 0.5 + 8 * 0.5) / 1e6)

        item = self.document_item()
        self.assertEqual(item["userId"], USER)
        self.assertEqual(item["jobId"], JOB)
        self.assertEqual(item["documentTitle"], "Hidden search frustrates address changes")
        self.assertEqual(item["storageKey"], f"documents/{USER}/{PROJECT}/{JOB}.md")
        self.assertEqual(item["contextFiles"], [{"fileName": "guide.txt", "status": "full"}])

        markdown = self.markdown()
        self.assertTrue(markdown.startswith("# Hidden search frustrates address changes\n"))
        self.assertIn("## Pain points", markdown)
        self.assertIn("> “the search bar was hidden”", markdown)
        self.assertIn("**Instruction:** Focus on the checkout flow.", markdown)

        result = self.saved("document")
        Draft202012Validator(DEFAULT_SCHEMA).validate(result["document"])

        [done] = self.events.delivered()
        self.assertEqual(done["detail-type"], "SynthesisDone")
        self.assertEqual(done["detail"]["eventId"], f"synthesisdone-{JOB}-0")
        self.assertEqual(done["detail"]["tokenCount"], int(job["tokenCount"]))

        rec = self.table.get_item(Key={"PK": f"PROJECT#{PROJECT}", "SK": f"REC#{RECORDING}"})["Item"]
        self.assertEqual(rec["transcriptKey"], handler.step_key(USER, JOB, "merged"))

        project = self.project_item()
        self.assertEqual(project["projectName"], "Study")
        self.assertEqual(project["lastDocumentAt"], item["createdAt"])

    def test_commit_uses_only_updates(self):
        # lambda-role is denied dynamodb:ConditionCheckItem and PutItem is unproven;
        # moto doesn't check IAM, so pin the transaction to Update parts, which need only UpdateItem.
        client = handler._client("table").meta.client
        with mock.patch.object(client, "transact_write_items", wraps=client.transact_write_items) as spy:
            self.assertEqual(self.run_handler(), {"batchItemFailures": []})
        [call] = spy.call_args_list
        self.assertEqual([list(part) for part in call.kwargs["TransactItems"]], [["Update"]] * 3)

    def test_merge_keeps_chunk_order_and_recording_times(self):
        self.run_handler()
        merged = self.saved("merged")
        self.assertEqual([s["start"] for s in merged["segments"]], [1.0, 6.0, 301.0, 307.0])
        self.assertEqual(merged["audioSeconds"], 600.0)

    def test_no_pii_reaches_the_synthesis_prompt_or_the_document(self):
        self.run_handler()
        synthesis_call = self.bedrock.calls[self.bedrock.tools_called().index("write_document")]
        prompt = json.dumps(synthesis_call["messages"])
        for value in PLANTED:
            self.assertNotIn(value, prompt)
            self.assertNotIn(value, self.markdown())
        redacted = self.saved("redacted")
        texts = [s["text"] for s in redacted["segments"]]
        self.assertIn("Sure, I'm [Person 1] and I'm a nurse at [Organisation A].", texts)
        self.assertIn("If you need more, [Person 1] is on [Phone 1] or [Email 1].", texts)
        self.assertEqual(redacted["entities"]["person"], 1)

    def test_prompt_carries_instruction_context_and_timestamps(self):
        self.run_handler()
        call = self.bedrock.calls[self.bedrock.tools_called().index("write_document")]
        prompt = call["messages"][0]["content"][0]["text"]
        self.assertIn("<instruction>\nFocus on the checkout flow.\n</instruction>", prompt)
        self.assertIn('<context_file name="guide.txt">\nDiscussion guide: ask about checkout.\n</context_file>', prompt)
        self.assertIn("[05:01] Changing my delivery address was confusing", prompt)
        self.assertEqual(call["toolConfig"]["toolChoice"], {"tool": {"name": "write_document"}})
        self.assertNotIn("$schema", call["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"])
        self.assertEqual(call["modelId"], bedrock.NEMOTRON)

    def test_context_text_is_saved_beside_the_file(self):
        self.run_handler()
        body = self.s3.get_object(Bucket=handler.UPLOADS_BUCKET, Key=CTX_KEY + ".txt")["Body"].read()
        self.assertEqual(body.decode(), "Discussion guide: ask about checkout.")


class TestIdempotency(Base):
    def test_a_second_request_after_done_only_resends_the_event(self):
        self.run_handler()
        calls = len(self.bedrock.calls)
        self.assertEqual(self.run_handler(), {"batchItemFailures": []})
        self.assertEqual(len(self.bedrock.calls), calls)
        first, second = self.events.delivered()
        self.assertEqual(first["detail"]["eventId"], second["detail"]["eventId"])
        self.assertEqual(first["detail"]["tokenCount"], second["detail"]["tokenCount"])

    def test_resumes_after_the_last_saved_step(self):
        self.run_handler(seconds=900)
        # Pretend the run died after redaction: the job is back at Redacting, the later outputs are gone.
        self.table.update_item(Key={"PK": f"JOB#{JOB}", "SK": "META"}, UpdateExpression="SET #s = :r",
                               ExpressionAttributeNames={"#s": "stage"}, ExpressionAttributeValues={":r": "Redacting"})
        self.s3.delete_object(Bucket=handler.UPLOADS_BUCKET, Key=handler.step_key(USER, JOB, "document"))
        self.table.delete_item(Key={"PK": f"PROJECT#{PROJECT}", "SK": f"DOC#{JOB}"})
        self.bedrock.calls.clear()

        self.run_handler()
        self.assertEqual(self.bedrock.tools_called(), ["write_document", "title"])  # no redaction call
        self.assertEqual(self.job()["stage"], "Done")
        # The redaction tokens from the first run still count.
        self.assertEqual(int(self.job()["tokenCount"]), 1100 + 1100 + 58)

    def test_out_of_time_releases_the_message_at_once(self):
        result = self.run_handler(seconds=200)  # under one call's read timeout plus margin
        self.assertEqual(len(result["batchItemFailures"]), 1)
        self.assertEqual(self.bedrock.calls, [])
        self.assertEqual(self.job()["stage"], "Redacting")
        self.assertEqual(self.saved("merged")["chunksTotal"], 2)  # the merge is kept
        self.assertEqual(self.visible_now(), 1)

    def test_saved_steps_from_another_chunking_are_ignored(self):
        self.run_handler()
        self.table.update_item(Key={"PK": f"JOB#{JOB}", "SK": "META"},
                               UpdateExpression="SET #s = :t, chunksTotal = :three",
                               ExpressionAttributeNames={"#s": "stage"},
                               ExpressionAttributeValues={":t": "Transcribing", ":three": 3})
        self.put_transcript(2, [{"start": 601.0, "end": 603.0, "text": "One more thing."}])
        self.bedrock.calls.clear()
        self.run_handler()
        self.assertEqual(self.bedrock.tools_called(), ["report_pii", "write_document", "title"])
        self.assertEqual(self.saved("merged")["chunksTotal"], 3)


class TestSkips(Base):
    def test_job_gone(self):
        self.table.delete_item(Key={"PK": f"JOB#{JOB}", "SK": "META"})
        self.assertEqual(self.run_handler(), {"batchItemFailures": []})
        self.assertEqual(self.bedrock.calls, [])

    def test_other_users_job(self):
        self.assertEqual(self.run_handler({"jobId": JOB, "userId": "someone-else"}), {"batchItemFailures": []})
        self.assertEqual(self.bedrock.calls, [])
        self.assertEqual(self.job()["stage"], "Transcribing")

    def test_claim_reset_by_a_retry(self):
        self.table.update_item(Key={"PK": f"JOB#{JOB}", "SK": "META"}, UpdateExpression="REMOVE synthesisEnqueued")
        self.run_handler()
        self.assertEqual(self.bedrock.calls, [])

    def test_failed_job(self):
        self.table.update_item(Key={"PK": f"JOB#{JOB}", "SK": "META"}, UpdateExpression="SET #s = :f",
                               ExpressionAttributeNames={"#s": "stage"}, ExpressionAttributeValues={":f": "Failed"})
        self.run_handler()
        self.assertEqual(self.bedrock.calls, [])
        self.assertEqual(self.events.entries, [])

    def test_bad_body_is_retried_towards_the_dlq(self):
        result = self.run_handler({"jobId": "a/b", "userId": USER})
        self.assertEqual(len(result["batchItemFailures"]), 1)


class TestFailures(Base):
    def test_invalid_output_is_regenerated_once(self):
        bad = {k: v for k, v in DOCUMENT.items() if k != "summary"}
        self.bedrock.documents = [bad, DOCUMENT]
        self.run_handler()
        self.assertEqual(self.bedrock.tools_called("write_document"), ["write_document"] * 2)
        second = self.bedrock.calls[self.bedrock.tools_called().index("write_document") + 1]
        result = second["messages"][-1]["content"][0]["toolResult"]
        self.assertEqual(result["toolUseId"], "t-write_document")
        self.assertIn("'summary' is a required property", result["content"][0]["text"])
        self.assertEqual(self.job()["stage"], "Done")

    def test_invalid_twice_fails_the_job_with_what_it_spent(self):
        self.bedrock.documents = [{"summary": 1}]
        self.assertEqual(self.run_handler(), {"batchItemFailures": []})
        job = self.job()
        self.assertEqual(job["stage"], "Failed")
        self.assertIn("didn't match the template", job["failureReason"])
        self.assertIsNone(self.document_item())
        [failed] = self.events.delivered()
        self.assertEqual(failed["detail-type"], "JobFailed")
        self.assertEqual(failed["detail"]["stage"], "Synthesising")
        self.assertEqual(failed["detail"]["tokenCount"], 1100 * 3)

    def test_no_speech(self):
        for n in range(2):
            self.put_transcript(n, [])
        self.run_handler()
        job = self.job()
        self.assertEqual((job["stage"], job["failureReason"][:24]), ("Failed", "No speech was found in t"))
        self.assertEqual(self.bedrock.calls, [])
        [failed] = self.events.delivered()
        self.assertEqual(failed["detail"]["stage"], "Transcribing")
        self.assertNotIn("tokenCount", failed["detail"])

    def test_template_gone(self):
        self.table.delete_item(Key={"PK": "SCHEMA#BUILTIN", "SK": "SCHEMA#default"})
        self.run_handler()
        self.assertEqual(self.job()["stage"], "Failed")
        self.assertIn("template", self.job()["failureReason"])

    def test_missing_transcript_backs_off(self):
        self.s3.delete_object(Bucket=handler.UPLOADS_BUCKET, Key=handler.chunk_transcript_key(USER, JOB, 1))
        result = self.run_handler()
        self.assertEqual(len(result["batchItemFailures"]), 1)
        self.assertEqual(self.job()["stage"], "Transcribing")
        self.assertEqual(self.visible_now(), 0)  # hidden for 60 s x 1, not released at once

    def test_title_failure_uses_the_fallback(self):
        self.bedrock.title_error = RuntimeError("ThrottlingException")
        self.run_handler()
        self.assertEqual(self.document_item()["documentTitle"], "General interview: interview-1.m4a")
        self.assertTrue(self.saved("document")["titleFallback"])

    def test_project_deleted_before_the_commit(self):
        self.table.delete_item(Key={"PK": f"USER#{USER}", "SK": f"PROJECT#{PROJECT}"})
        self.assertEqual(self.run_handler(), {"batchItemFailures": []})
        self.assertIsNone(self.document_item())
        self.assertIsNone(self.project_item())  # the conditional update didn't recreate the project
        self.assertFalse(self.exists(handler.DOCS_BUCKET, handler.document_key(USER, PROJECT, JOB)))
        self.assertEqual(self.events.entries, [])


class TestContextFiles(Base):
    def add_context(self, ctx_id, file_name, ext, body=None):
        key = f"context/{USER}/{PROJECT}/{ctx_id}.{ext}"
        self.table.put_item(Item={"PK": f"PROJECT#{PROJECT}", "SK": f"CTX#{ctx_id}", "fileName": file_name, "storageKey": key})
        self.table.put_item(Item={"PK": f"JOB#{JOB}", "SK": f"CTX#{ctx_id}"})
        if body is not None:
            self.s3.put_object(Bucket=handler.UPLOADS_BUCKET, Key=key, Body=body)

    def test_budget_truncates_then_omits_and_records_it(self):
        handler.CONTEXT_CHAR_BUDGET = 50
        self.addCleanup(setattr, handler, "CONTEXT_CHAR_BUDGET", 40000)
        self.add_context(CTX[:-1] + "b", "notes.md", "md", b"x" * 100)
        self.add_context(CTX[:-1] + "c", "later.txt", "txt", b"more")
        self.add_context(CTX[:-1] + "d", "never.pdf", "pdf")  # presigned, never uploaded
        self.add_context(CTX[:-1] + "e", "broken.pdf", "pdf", b"not a pdf")
        self.run_handler()
        self.assertEqual(self.document_item()["contextFiles"], [
            {"fileName": "guide.txt", "status": "full"},
            {"fileName": "notes.md", "status": "truncated"},
            {"fileName": "later.txt", "status": "omitted"},
            {"fileName": "never.pdf", "status": "missing"},
            {"fileName": "broken.pdf", "status": "unreadable"},
        ])
        self.assertIn("**Context files:** guide.txt, notes.md (truncated), later.txt (omitted)", self.markdown())

    def test_a_key_outside_the_users_prefix_is_not_read(self):
        self.table.update_item(Key={"PK": f"PROJECT#{PROJECT}", "SK": f"CTX#{CTX}"},
                               UpdateExpression="SET storageKey = :k",
                               ExpressionAttributeValues={":k": f"context/someone-else/{PROJECT}/{CTX}.txt"})
        self.run_handler()
        self.assertEqual(self.document_item()["contextFiles"], [{"fileName": "guide.txt", "status": "missing"}])


class TestLogs(Base):
    """M16: the fields the trace query reads, and the metrics the dashboard reads, from the EMF lines."""

    def metrics(self, record):
        [directive] = record["_aws"]["CloudWatchMetrics"]
        return {m["Name"]: record[m["Name"]] for m in directive["Metrics"]}, directive["Dimensions"]

    def test_every_line_names_the_job_and_each_step_is_timed(self):
        self.table.update_item(Key={"PK": f"JOB#{JOB}", "SK": "META"}, UpdateExpression="SET createdAt = :t",
                               ExpressionAttributeValues={":t": "2026-01-01T00:00:00.000Z"})
        with logs.capture() as records:
            self.run_handler()
        for record in records:
            self.assertEqual((record["service"], record["jobId"], record["userId"]), ("synthesis", JOB, USER))
            self.assertIn("messageId", record)
        by_msg = {r["msg"]: r for r in records}
        for msg, step, stage in (("merged", "merge", "Transcribing"), ("redacted", "redact", "Redacting"),
                                 ("synthesised", "synthesise", "Synthesising")):
            values, dimensions = self.metrics(by_msg[msg])
            self.assertEqual((by_msg[msg]["stage"], by_msg[msg]["Step"], dimensions), (stage, step, [["Step"], []]))
            self.assertEqual(values["StepDuration"], by_msg[msg]["durationMs"])
        values, _ = self.metrics(by_msg["done"])
        self.assertEqual((by_msg["done"]["stage"], values["JobsDone"]), ("Done", 1))
        self.assertEqual(values["JobDuration"], by_msg["done"]["durationMs"])
        self.assertGreater(values["JobDuration"], 0)
        # The title is model output about the recording: not logged.
        self.assertNotIn("Hidden search", json.dumps(records, default=str))

    def test_each_bedrock_call_is_metered_by_model(self):
        with logs.capture() as records:
            self.run_handler()
        calls = [r for r in records if "LlmTokens" in r]
        self.assertEqual([(r["Model"], r["stage"]) for r in calls], [
            (bedrock.NEMOTRON, "Redacting"), (bedrock.NEMOTRON, "Synthesising"), (bedrock.GEMMA, "Synthesising"),
        ])
        self.assertEqual(sum(r["LlmTokens"] for r in calls), int(self.job()["tokenCount"]))
        self.assertAlmostEqual(sum(r["LlmCost"] for r in calls), float(self.job()["cost"]))

    def test_a_failed_job_and_an_error_are_counted(self):
        self.bedrock.documents = [{"summary": 1}]
        with logs.capture() as records:
            self.run_handler()
        [failed] = [r for r in records if r["msg"] == "failed"]
        self.assertEqual((failed["stage"], self.metrics(failed)[0]), ("Synthesising", {"JobsFailed": 1}))

        self.s3.delete_object(Bucket=handler.UPLOADS_BUCKET, Key=handler.chunk_transcript_key(USER, JOB, 1))
        self.table.update_item(Key={"PK": f"JOB#{JOB}", "SK": "META"}, UpdateExpression="SET #s = :s",
                               ExpressionAttributeNames={"#s": "stage"}, ExpressionAttributeValues={":s": "Transcribing"})
        for step in ("merged", "redacted", "document"):
            self.s3.delete_object(Bucket=handler.UPLOADS_BUCKET, Key=handler.step_key(USER, JOB, step))
        with logs.capture() as records:
            self.run_handler()
        [error] = [r for r in records if r["level"] == "ERROR"]
        self.assertEqual((error["Service"], error["Errors"], error["jobId"]), ("synthesis", 1, JOB))
        self.assertIn("transcript of chunk 1 is missing", error["trace"])


if __name__ == "__main__":
    unittest.main()
