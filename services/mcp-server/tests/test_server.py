"""MCP server tests: the real server over Streamable HTTP with real token checks, against moto. Run with
`make test-mcp-server` (after `make venv-chat`).

Every call goes through the MCP client the agent uses, with the caller's ID token as the bearer, so these cover the
transport, the per-call token check and the user scoping of every tool together.
"""

import asyncio
import json
import unittest
from unittest import mock

import httpx2
from contracts import logs
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from moto import mock_aws

from mcp_server import auth, server, store
from tests import fixtures
from tests.fixtures import ALICE, ALICE_DOC_1, ALICE_PROJECT, BOB, BOB_DOC, BOB_PROJECT, BOB_SECRET


async def _session(token, work):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=10) as http,
        Client(streamable_http_client(fixtures.mcp_server_url(), http_client=http), cache=None) as client,
    ):
        return await work(client)


def call(name, arguments=None, user=ALICE, token=None):
    """(is_error, parsed JSON or the error text) of one tool call as user."""
    token = token if token is not None else fixtures.id_token(user)

    async def work(client):
        return await client.call_tool(name, arguments or {})

    result = asyncio.run(_session(token, work))
    text = "\n".join(block.text for block in result.content)
    return result.is_error, (text if result.is_error else json.loads(text))


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        mock = mock_aws()
        mock.start()
        self.addCleanup(mock.stop)
        for cached in (store.table, store.s3, auth.jwks_client):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        fixtures.patch_jwks(self)
        fixtures.create_resources()
        fixtures.seed()
        self.records = self.enterContext(logs.capture())


class Tools(ServerTestCase):
    def test_no_tool_takes_a_user(self):
        tools = asyncio.run(_session(fixtures.id_token(), lambda c: c.list_tools())).tools
        self.assertEqual({t.name for t in tools},
                         {"list_projects", "list_documents", "read_document", "get_history", "get_job_stats"})
        for tool in tools:
            properties = tool.input_schema.get("properties", {})
            self.assertFalse([p for p in properties if "user" in p.lower()], tool.name)
            self.assertTrue(tool.annotations.read_only_hint, tool.name)

    def test_list_projects_is_the_callers_only(self):
        error, alice = call("list_projects")
        self.assertFalse(error)
        self.assertEqual([p["projectId"] for p in alice["projects"]], [ALICE_PROJECT])
        self.assertEqual(alice["projects"][0]["name"], "Checkout redesign")
        _, bob = call("list_projects", user=BOB)
        self.assertEqual([p["projectId"] for p in bob["projects"]], [BOB_PROJECT])

    def test_list_documents_with_what_isnt_ready(self):
        error, result = call("list_documents", {"projectId": ALICE_PROJECT})
        self.assertFalse(error)
        self.assertEqual(result["projectName"], "Checkout redesign")
        self.assertEqual(sorted(d["title"] for d in result["documents"]),
                         ["Checkout usability test — Participant 3", "Lab session — Participant 7"])
        self.assertEqual(result["notReady"], [{"fileName": "p4.m4a", "stage": "Transcribing"}])

    def test_list_documents_across_projects_in_one_call(self):
        fixtures.table().put_item(Item={"PK": f"USER#{ALICE}", "SK": "PROJECT#p2", "projectName": "Nurse ordering"})
        fixtures.put_document(ALICE, "p2", "d9", "Ward round — Nurse 2", "# Ward round")
        error, result = call("list_documents")
        self.assertFalse(error)
        self.assertEqual(result["count"], 3)
        self.assertEqual(sorted((d["projectName"], d["title"]) for d in result["documents"]), [
            ("Checkout redesign", "Checkout usability test — Participant 3"),
            ("Checkout redesign", "Lab session — Participant 7"),
            ("Nurse ordering", "Ward round — Nurse 2"),
        ])
        # Each entry carries both IDs, so read_document can follow straight on.
        nurse = next(d for d in result["documents"] if d["projectName"] == "Nurse ordering")
        self.assertEqual((nurse["projectId"], nurse["documentId"]), ("p2", "d9"))
        self.assertEqual(result["notReady"], [{"projectName": "Checkout redesign", "fileName": "p4.m4a",
                                               "stage": "Transcribing"}])
        self.assertNotIn("truncated", result)

    def test_list_documents_across_projects_is_capped(self):
        for n in range(3):
            fixtures.put_document(ALICE, ALICE_PROJECT, f"extra-{n}", f"Extra {n}", "x")
        with mock.patch.object(server, "MAX_DOCUMENTS_LISTED", 2):
            _, result = call("list_documents")
        self.assertEqual((len(result["documents"]), result["count"]), (2, 5))
        self.assertIn("newest 2 of 5", result["truncated"])

    def test_read_document(self):
        error, result = call("read_document", {"projectId": ALICE_PROJECT, "documentId": ALICE_DOC_1})
        self.assertFalse(error)
        self.assertEqual(result["title"], "Checkout usability test — Participant 3")
        self.assertIn("Delivery fees appear late", result["markdown"])
        self.assertNotIn("truncated", result)

    def test_read_document_with_the_keys_list_documents_gave(self):
        """Issue #33: the model copies {"projectId", "documentId"} from the list result, so those are the inputs."""
        _, listed = call("list_documents")
        error, result = call("read_document", {k: listed["documents"][0][k] for k in ("projectId", "documentId")})
        self.assertFalse(error, result)
        self.assertEqual(result["documentId"], listed["documents"][0]["documentId"])

    def test_long_document_is_cut(self):
        fixtures.put_document(ALICE, ALICE_PROJECT, "long", "Long one", "x" * (server.MAX_DOCUMENT_CHARS + 10))
        _, result = call("read_document", {"projectId": ALICE_PROJECT, "documentId": "long"})
        self.assertEqual(len(result["markdown"]), server.MAX_DOCUMENT_CHARS)
        self.assertIn("cut at", result["truncated"])

    def test_history_newest_first_and_limited(self):
        _, result = call("get_history", {"limit": 1})
        self.assertEqual([e["eventType"] for e in result["events"]], ["SynthesisDone"])
        self.assertEqual(result["events"][0]["cost"], 0.04)
        _, result = call("get_history")
        self.assertEqual([e["eventType"] for e in result["events"]], ["SynthesisDone", "UploadReceived"])

    def test_job_stats(self):
        _, result = call("get_job_stats")
        [project] = result["projects"]
        self.assertEqual(project["jobsByStage"], {"Done": 2, "Transcribing": 1})
        self.assertEqual(project["audioMinutes"], 60.0)
        self.assertEqual(project["tokenCount"], 24000)


class UserScoping(ServerTestCase):
    """Bob can't reach Alice's data through any tool, whatever IDs he (or a prompt-injected model) passes."""

    def test_someone_elses_project(self):
        error, text = call("list_documents", {"projectId": ALICE_PROJECT}, user=BOB)
        self.assertTrue(error)
        self.assertIn("No such", text)
        self.assertNotIn("Checkout", text)

    def test_someone_elses_document(self):
        for project in (ALICE_PROJECT, BOB_PROJECT):  # the right project, and Bob's own with Alice's document
            error, text = call("read_document", {"projectId": project, "documentId": ALICE_DOC_1}, user=BOB)
            self.assertTrue(error)
            self.assertNotIn("Delivery", text)
        error, text = call("read_document", {"projectId": BOB_PROJECT, "documentId": BOB_DOC}, user=ALICE)
        self.assertTrue(error)
        self.assertNotIn(BOB_SECRET, text)

    def test_key_delimiters_in_ids_are_refused(self):
        error, text = call("read_document", {"projectId": f"{ALICE_PROJECT}#x", "documentId": ALICE_DOC_1})
        self.assertTrue(error)
        self.assertIn("No such projectId", text)

    def test_documents_across_projects_are_the_callers(self):
        _, bob = call("list_documents", user=BOB)
        self.assertEqual([d["title"] for d in bob["documents"]], ["Onboarding interview"])
        self.assertNotIn("Checkout", json.dumps(bob))
        _, alice = call("list_documents")
        self.assertNotIn("Onboarding", json.dumps(alice))

    def test_history_and_stats_are_the_callers(self):
        _, history = call("get_history", user=BOB)
        self.assertEqual([e["eventType"] for e in history["events"]], ["UploadReceived"])
        _, stats = call("get_job_stats", user=BOB)
        self.assertEqual([p["projectId"] for p in stats["projects"]], [BOB_PROJECT])


class Tokens(ServerTestCase):
    def test_no_token(self):
        error, text = call("list_projects", token="")
        self.assertTrue(error)
        self.assertIn("Not signed in", text)

    def test_tokens_that_fail_a_check(self):
        for token in (
            fixtures.id_token(token_use="access"),
            fixtures.id_token(aud="someone-else"),
            fixtures.id_token(exp=1),
            fixtures.id_token(sub="USER#x"),
            fixtures.id_token() + "x",
        ):
            error, text = call("list_projects", token=token)
            self.assertTrue(error)
            self.assertIn("Not signed in", text)

    def test_every_call_logs_one_line_without_content(self):
        call("read_document", {"projectId": ALICE_PROJECT, "documentId": ALICE_DOC_1})
        call("list_projects", token="")
        lines = [r for r in self.records if r["msg"] == "tool call"]
        self.assertEqual([(r["Tool"], r["outcome"], r["userId"]) for r in lines],
                         [("read_document", "ok", ALICE), ("list_projects", "unauthorized", None)])
        self.assertNotIn("Delivery", json.dumps(self.records))
        self.assertIn("ToolLatency", lines[0])


if __name__ == "__main__":
    unittest.main()
