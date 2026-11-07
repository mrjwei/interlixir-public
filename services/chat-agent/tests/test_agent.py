"""Chat agent tests: the /acp routes end to end against moto, with the real MCP server (uvicorn in a thread), real
token checks, a scripted Bedrock and an in-memory Memcached. Run with `make test-chat-agent` (after `make venv-chat`).

The scripted model asks for the tool calls a real one would; everything after it is real: the agent's tool loop,
the MCP transport with the caller's token, the server's user scoping, the session writes, the quota and the
streamed events. So "user B can't see user A's documents" is tested as the done-when states it: B's model asks for
A's IDs, and what comes back (to the model and to B) holds none of A's content.
"""

import importlib.util
import pathlib

# The MCP server's fixtures (fake AWS credentials first, then the seeded table and the token key). Loaded by path:
# both services' test folders are packages named `tests`.
_spec = importlib.util.spec_from_file_location(
    "mcp_fixtures", pathlib.Path(__file__).parents[2] / "mcp-server" / "tests" / "fixtures.py"
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)

import copy
import datetime
import json
import threading
import time
import unittest
from unittest import mock

import mcp_server.auth
import mcp_server.store
from contracts import events as contract, logs
from fastapi.testclient import TestClient
from moto import mock_aws

from agent import auth, chat, main, quota, store

ALICE, BOB = fixtures.ALICE, fixtures.BOB
PRICES = json.dumps({chat.MODEL_ID: {"input": 0.15, "output": 0.65}})


# --- fakes ------------------------------------------------------------------------------------------


class FakeBedrock:
    """converse() returns the scripted replies in order (a callable reply gets the request). Each request is kept."""

    class exceptions:
        class ThrottlingException(Exception):
            pass

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def converse(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        reply = self.replies.pop(0) if self.replies else answer("(script ran out)")
        if isinstance(reply, Exception):
            raise reply
        return reply(kwargs) if callable(reply) else reply

    def tool_results(self, n):
        """The text of every toolResult the model was sent in request n."""
        return [c["text"] for m in self.requests[n]["messages"] for b in m["content"] if "toolResult" in b
                for c in b["toolResult"]["content"]]


def tools(*calls):
    content = [{"toolUse": {"toolUseId": f"tu-{i}", "name": name, "input": args}} for i, (name, args) in enumerate(calls)]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use",
            "usage": {"inputTokens": 1000, "outputTokens": 40}, "metrics": {"latencyMs": 900}}


def answer(text):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": "end_turn",
            "usage": {"inputTokens": 2000, "outputTokens": 200}, "metrics": {"latencyMs": 1500}}


class FakeMemcache:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def add(self, key, value, expire=0, noreply=None):
        if key in self.data:
            return False
        self.data[key] = value
        return True

    def incr(self, key, value, noreply=False):
        if key not in self.data:
            return None
        self.data[key] = str(int(self.data[key]) + value).encode()
        return int(self.data[key])


def sse(response):
    """[(event, data)] from a text/event-stream body; `: ping` comments dropped."""
    out = []
    for block in response.text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        out.append((fields["event"], json.loads(fields["data"])))
    return out


# --- base ---------------------------------------------------------------------------------------------


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        for cached in (store.table, store.events_client, auth.jwks_client, chat.bedrock, quota.client,
                       mcp_server.store.table, mcp_server.store.s3, mcp_server.auth.jwks_client):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        fixtures.patch_jwks(self)
        fixtures.create_resources()
        fixtures.seed()

        self.patch(chat, "MCP_URL", fixtures.mcp_server_url())
        self.bedrock = FakeBedrock()
        self.patch(chat, "bedrock", lambda: self.bedrock)
        self.memcache = FakeMemcache()
        self.patch(quota, "client", lambda: self.memcache)
        self.patch(quota, "_down_until", 0.0)
        self.published = []
        self.patch(store, "events_client", lambda: mock.Mock(put_events=self._put_events))
        self.enterContext(mock.patch.dict("os.environ", {"MODEL_PRICES": PRICES, "MODEL_PRICES_PARAMETER": ""}))
        self.records = self.enterContext(logs.capture())
        self.client = TestClient(main.app)

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _put_events(self, Entries):
        self.published.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e"} for _ in Entries]}

    def headers(self, user=ALICE):
        return {"Authorization": f"Bearer {fixtures.id_token(user)}"}

    def send(self, text, session_id=None, user=ALICE, *replies):
        self.bedrock.replies.extend(replies)
        body = {"text": text, **({"sessionId": session_id} if session_id else {})}
        return self.client.post("/acp/messages", json=body, headers=self.headers(user))

    def turn(self, text, *replies, session_id=None, user=ALICE):
        response = self.send(text, session_id, user, *replies)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        return sse(response)


def by_event(stream, name):
    return [data for event, data in stream if event == name]


# --- tests ---------------------------------------------------------------------------------------------


class ToolUse(AgentTestCase):
    def test_answers_by_calling_tools(self):
        """The done-when's question: the agent lists, then reads, then answers from what it read."""
        stream = self.turn(
            "Which themes recur across my usability tests?",
            tools(("list_projects", {})),
            tools(("list_documents", {"projectId": fixtures.ALICE_PROJECT})),
            tools(("read_document", {"projectId": fixtures.ALICE_PROJECT, "documentId": fixtures.ALICE_DOC_1}),
                  ("read_document", {"projectId": fixtures.ALICE_PROJECT, "documentId": fixtures.ALICE_DOC_2})),
            answer("Costs that appear late come up in both tests."),
        )
        self.assertEqual([e for e, _ in stream][:2], ["session", "quota"])
        self.assertEqual([e for e, _ in stream][-2:], ["message", "done"])
        done = [t for t in by_event(stream, "tool") if t["status"] == "done"]
        self.assertEqual([(t["name"], t.get("label")) for t in done], [
            ("list_projects", None),
            ("list_documents", "Checkout redesign"),
            ("read_document", "Checkout usability test — Participant 3"),
            ("read_document", "Lab session — Participant 7"),
        ])
        [message] = by_event(stream, "message")
        self.assertEqual(message["text"], "Costs that appear late come up in both tests.")
        self.assertEqual(message["tokenCount"], 3 * 1040 + 2200)

        # The model was sent the real tool results: Alice's project, then both documents' text.
        self.assertIn("Checkout redesign", self.bedrock.tool_results(1)[0])
        self.assertTrue(any("Delivery fees appear late" in t for t in self.bedrock.tool_results(3)))
        self.assertTrue(any("Delivery costs surprised" in t for t in self.bedrock.tool_results(3)))
        # The model never sees a userId: not in the tools, the prompt or the arguments it gets back.
        self.assertNotIn(ALICE, json.dumps(self.bedrock.requests))
        for spec in self.bedrock.requests[0]["toolConfig"]["tools"]:
            properties = spec["toolSpec"]["inputSchema"]["json"].get("properties", {})
            self.assertFalse([p for p in properties if "user" in p.lower()], spec["toolSpec"]["name"])

    def test_a_question_across_many_projects_reads_within_the_cap(self):
        """Issue #30: with a round per project, discovery used up every round. Now one list_documents call gives
        both IDs of every document, and the model reads them in the rounds left. It copies the keys as listed
        (issue #33)."""
        for n in range(12):
            fixtures.table().put_item(Item={"PK": f"USER#{ALICE}", "SK": f"PROJECT#p{n}", "projectName": f"Study {n}"})
            fixtures.put_document(ALICE, f"p{n}", f"d{n}", f"Session {n}", f"# Session {n}\n\nFinding {n}.")

        def read_newest(n):
            def reply(request):
                listed = json.loads(next(b["toolResult"]["content"][0]["text"] for m in request["messages"]
                                         for b in m["content"] if "toolResult" in b))
                doc = listed["documents"][n]
                return tools(("read_document", {k: doc[k] for k in ("projectId", "documentId")}))
            return reply

        rounds = chat.MAX_TOOL_ROUNDS - 1
        stream = self.turn("Which themes recur across my usability tests?", tools(("list_documents", {})),
                           *[read_newest(n) for n in range(rounds)], answer("Themes."))
        done = [t for t in by_event(stream, "tool") if t["status"] == "done"]
        self.assertEqual([t["name"] for t in done], ["list_documents"] + ["read_document"] * rounds)
        self.assertEqual(sum("Finding" in t for t in self.bedrock.tool_results(-1)), rounds)
        self.assertEqual(by_event(stream, "message")[0]["text"], "Themes.")

    def test_a_rejected_call_logs_the_argument_names_not_values(self):
        """Issue #33: the server's validation error never reached a log, so nothing showed what the model sent."""
        self.turn("Read it", tools(("read_document", {"project_id": fixtures.ALICE_PROJECT,
                                                      "document_id": fixtures.ALICE_DOC_1})), answer("I couldn't."))
        [line] = [r for r in self.records if r["msg"] == "tool result"]
        self.assertEqual((line["ok"], line["args"]), (False, ["document_id", "project_id"]))
        self.assertNotIn(fixtures.ALICE_DOC_1, json.dumps(line))

    def test_saves_the_session_and_publishes_the_turn(self):
        stream = self.turn("Summarise my projects", tools(("list_projects", {})), answer("You have one project."))
        session_id = by_event(stream, "session")[0]["sessionId"]
        self.assertTrue(by_event(stream, "session")[0]["created"])

        session = self.client.get(f"/acp/sessions/{session_id}", headers=self.headers()).json()
        self.assertEqual(session["title"], "Summarise my projects")
        self.assertEqual(session["messageCount"], 2)
        self.assertEqual([(m["role"], m["text"]) for m in session["messages"]],
                         [("user", "Summarise my projects"), ("assistant", "You have one project.")])
        self.assertEqual(session["messages"][1]["tools"], [{"name": "list_projects", "ok": True}])
        self.assertEqual(session["tokenCount"], 1040 + 2200)
        self.assertAlmostEqual(session["cost"], (3000 * 0.15 + 240 * 0.65) / 1e6, places=9)

        [entry] = self.published
        event = {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": json.loads(entry["Detail"])}
        contract.validate_event(event)
        self.assertEqual(event["detail"]["chatSessionId"], session_id)
        self.assertEqual(event["detail"]["eventId"], session["messages"][1]["messageId"])
        self.assertEqual(event["detail"]["tokenCount"], 3240)

    def test_a_reopened_session_sends_its_history(self):
        first = self.turn("Which projects do I have?", answer("Checkout redesign."))
        session_id = by_event(first, "session")[0]["sessionId"]
        second = self.turn("And how many documents in it?", answer("Two."), session_id=session_id)
        self.assertFalse(by_event(second, "session")[0]["created"])
        sent = self.bedrock.requests[1]["messages"]
        self.assertEqual([(m["role"], m["content"][0]["text"]) for m in sent], [
            ("user", "Which projects do I have?"), ("assistant", "Checkout redesign."),
            ("user", "And how many documents in it?"),
        ])
        listed = self.client.get("/acp/sessions", headers=self.headers()).json()["sessions"]
        self.assertEqual([(s["sessionId"], s["messageCount"]) for s in listed], [(session_id, 4)])

    def test_an_unknown_tool_is_a_tool_error_not_a_failed_turn(self):
        stream = self.turn("Hi", tools(("delete_everything", {})), answer("I can't do that."))
        self.assertTrue(self.bedrock.tool_results(1)[0].startswith("Error: "))
        self.assertEqual(by_event(stream, "message")[0]["text"], "I can't do that.")

    def test_tool_rounds_are_capped(self):
        endless = [tools(("list_projects", {}))] * (chat.MAX_TOOL_ROUNDS + 1)
        stream = self.turn("Loop forever", *endless)
        self.assertEqual(len(self.bedrock.requests), chat.MAX_TOOL_ROUNDS + 1)
        last = self.bedrock.requests[-1]["messages"][-1]["content"]
        self.assertEqual(last[-1], {"text": chat.FORCE_ANSWER})
        self.assertIn("only if you read it with read_document", chat.FORCE_ANSWER)
        self.assertEqual(len(by_event(stream, "message")), 1)

    def test_a_model_failure_is_an_error_event_and_keeps_the_question(self):
        stream = self.turn("Hello", RuntimeError("bedrock down"))
        self.assertEqual([e for e, _ in stream][-2:], ["error", "done"])
        session_id = by_event(stream, "session")[0]["sessionId"]
        messages = self.client.get(f"/acp/sessions/{session_id}", headers=self.headers()).json()["messages"]
        self.assertEqual([m["role"] for m in messages], ["user"])
        self.assertEqual(self.published, [])

    def test_heartbeats_while_the_model_works(self):
        self.patch(main, "HEARTBEAT_SECONDS", 0.05)

        def slow(request):
            time.sleep(0.3)
            return answer("done")

        response = self.send("Hello", None, ALICE, slow)
        self.assertIn(": ping\n\n", response.text)
        self.assertEqual([e for e, _ in sse(response)][-2:], ["message", "done"])

    def test_the_model_is_called_on_its_own_threads(self):
        # Issue #45: hung Converse calls on the default executor starved the session and quota reads.
        threads = []

        def record(request):
            threads.append(threading.current_thread().name)
            return answer("done")

        self.turn("Hello", record)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith("bedrock"), threads)

    def test_throttling_says_busy(self):
        stream = self.turn("Hello", FakeBedrock.exceptions.ThrottlingException())
        self.assertIn("busy", by_event(stream, "error")[0]["detail"])


class Isolation(AgentTestCase):
    """User B's session can't see user A's docs (the done-when)."""

    def test_bobs_model_asking_for_alices_document_gets_nothing(self):
        stream = self.turn(
            "Read document " + fixtures.ALICE_DOC_1,
            tools(("read_document", {"projectId": fixtures.ALICE_PROJECT, "documentId": fixtures.ALICE_DOC_1}),
                  ("list_documents", {"projectId": fixtures.ALICE_PROJECT})),
            tools(("list_projects", {})),
            answer("I can't find that document."),
            user=BOB,
        )
        results = self.bedrock.tool_results(1)
        self.assertTrue(all(r.startswith("Error: ") for r in results), results)
        sent = json.dumps(self.bedrock.requests)
        for alice_content in ("Delivery", "Checkout", "Participant 3", ALICE):
            self.assertNotIn(alice_content, sent)
        self.assertIn("Onboarding research", self.bedrock.tool_results(2)[-1])  # his own project, through the same path
        self.assertEqual([t["status"] for t in by_event(stream, "tool") if t["status"] != "running"],
                         ["error", "error", "done"])

    def test_sessions_are_per_user(self):
        stream = self.turn("Alice's question", answer("Alice's answer"))
        session_id = by_event(stream, "session")[0]["sessionId"]
        for method in ("get", "delete"):
            response = getattr(self.client, method)(f"/acp/sessions/{session_id}", headers=self.headers(BOB))
            self.assertEqual(response.status_code, 404, method)
        self.assertEqual(self.client.get("/acp/sessions", headers=self.headers(BOB)).json(), {"sessions": []})
        # Posting into Alice's session as Bob: 404, before the quota is touched.
        response = self.send("sneak in", session_id, BOB)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/acp/quota", headers=self.headers(BOB)).json()["used"], 0)
        self.assertEqual(len(self.client.get(f"/acp/sessions/{session_id}", headers=self.headers()).json()["messages"]), 2)

    def test_every_route_needs_a_valid_token(self):
        for method, path in (("get", "/acp/quota"), ("get", "/acp/sessions"), ("get", "/acp/sessions/x"),
                             ("delete", "/acp/sessions/x"), ("post", "/acp/messages")):
            for headers in ({}, {"Authorization": "Bearer " + fixtures.id_token(token_use="access")}):
                kwargs = {"json": {"text": "hi"}} if method == "post" else {}
                response = getattr(self.client, method)(path, headers=headers, **kwargs)
                self.assertEqual(response.status_code, 401, (method, path, headers))
        self.assertEqual(self.bedrock.requests, [])

    def test_ids_with_key_delimiters_are_refused(self):
        response = self.client.get("/acp/sessions/a%23b", headers=self.headers())
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/acp/messages", json={"text": "hi", "sessionId": "a#b"}, headers=self.headers())
        self.assertEqual(response.status_code, 422)


class Sessions(AgentTestCase):
    def test_delete_removes_the_session_and_its_messages(self):
        stream = self.turn("To be deleted", answer("ok"))
        session_id = by_event(stream, "session")[0]["sessionId"]
        response = self.client.delete(f"/acp/sessions/{session_id}", headers=self.headers())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(f"/acp/sessions/{session_id}", headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get("/acp/sessions", headers=self.headers()).json(), {"sessions": []})
        left = fixtures.table().query(KeyConditionExpression="PK = :pk",
                                      ExpressionAttributeValues={":pk": f"CHAT#{session_id}"})["Items"]
        self.assertEqual(left, [])

    def test_an_answer_for_a_session_deleted_mid_turn_is_dropped(self):
        def delete_then_answer(request):
            session = store.list_sessions(ALICE)[0]
            store.delete_session(ALICE, session["SK"].removeprefix("CHAT#"))
            return answer("too late")

        stream = self.turn("Hello", delete_then_answer)
        self.assertIn("deleted", by_event(stream, "error")[0]["detail"])
        self.assertEqual(fixtures.table().scan(FilterExpression="begins_with(PK, :c)",
                                               ExpressionAttributeValues={":c": "CHAT#"})["Items"], [])

    def test_title_from_the_first_message(self):
        self.assertEqual(store.title_from("  Which themes\nrecur? "), "Which themes recur?")
        long = "Which themes recur across my usability tests and interviews from the last quarter please"
        title = store.title_from(long)
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), store.TITLE_CHARS + 1)

    def test_history_merges_an_unanswered_question(self):
        prior = [{"role": "assistant", "text": "orphan"}, {"role": "user", "text": "q1"}, {"role": "user", "text": "q2"},
                 {"role": "assistant", "text": "a2"}]
        self.assertEqual(chat.history_messages(prior), [
            {"role": "user", "content": [{"text": "q1\n\nq2"}]},
            {"role": "assistant", "content": [{"text": "a2"}]},
        ])


class Quota(AgentTestCase):
    def test_warns_at_80_percent_and_blocks_at_30(self):
        for n in range(1, quota.LIMIT + 1):
            [status] = by_event(self.turn(f"message {n}", answer("ok")), "quota")
            self.assertEqual(status["used"], n)
            self.assertEqual(status["warning"], n >= 24, n)
            self.assertEqual(status["blocked"], n == 30, n)
        response = self.send("message 31")
        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["quota"]["used"], 30)
        self.assertTrue(body["quota"]["blocked"])
        reset = datetime.datetime.fromisoformat(body["quota"]["resetAt"].replace("Z", "+00:00"))
        self.assertEqual((reset.minute, reset.second), (0, 0))
        self.assertGreater(int(response.headers["Retry-After"]), 0)
        self.assertEqual(len(self.bedrock.requests), 30)  # the blocked message never reached the model
        peek = self.client.get("/acp/quota", headers=self.headers()).json()
        self.assertEqual((peek["used"], peek["blocked"]), (30, True))
        # Per user: Bob still has his whole hour.
        self.assertEqual(self.client.get("/acp/quota", headers=self.headers(BOB)).json()["used"], 0)

    def test_window_and_key(self):
        now = datetime.datetime(2026, 10, 22, 14, 37, 5, tzinfo=datetime.timezone.utc)
        start, reset = quota.window(now)
        self.assertEqual(quota.key(ALICE, start), f"quota:{ALICE}:2026102214")
        self.assertEqual(reset, datetime.datetime(2026, 10, 22, 15, tzinfo=datetime.timezone.utc))

    def test_without_a_cache_it_fails_open(self):
        self.patch(quota, "client", lambda: None)
        stream = self.turn("Hello", answer("hi"))
        self.assertEqual(by_event(stream, "quota")[0]["used"], None)
        self.assertEqual(len(by_event(stream, "message")), 1)
        self.assertTrue(any(r["msg"] == "quota unavailable, message not metered" for r in self.records))


if __name__ == "__main__":
    unittest.main()
