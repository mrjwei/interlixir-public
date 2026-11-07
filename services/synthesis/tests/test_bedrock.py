"""bedrock.structured on both paths (forced tool, JSON in text), the one regeneration, usage and cost, the clock."""

import json
import unittest

import bedrock
from contracts import logs

TOOL = {"name": "report", "description": "d", "schema": {"type": "object", "required": ["n"],
                                                         "properties": {"n": {"type": "integer"}}}}


def validate(obj):
    return [] if isinstance(obj.get("n"), int) else ["n: not an integer"]


class Scripted:
    """Replies in order; records the requests."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        content, stop = self.replies.pop(0)
        return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop,
                "usage": {"inputTokens": 100, "outputTokens": 10}, "metrics": {"latencyMs": 5}}


def tool_use(value, id_="t1"):
    return ([{"reasoningContent": {"reasoningText": {"text": "hmm"}}},
             {"toolUse": {"toolUseId": id_, "name": "report", "input": value}}], "tool_use")


class Context:
    def __init__(self, left):
        self.left = left

    def get_remaining_time_in_millis(self):
        return self.left * 1000


def clock(left=900):
    return bedrock.Clock(Context(left))


class TestStructured(unittest.TestCase):
    def call(self, client, model=bedrock.NEMOTRON, usage=None):
        return bedrock.structured(client, model, "rules", "data", TOOL, validate, usage or bedrock.Usage(),
                                  clock(), max_tokens=100)

    def test_forced_tool(self):
        client = Scripted(tool_use({"n": 3}))
        self.assertEqual(self.call(client), {"n": 3})
        request = client.requests[0]
        self.assertEqual(request["toolConfig"]["toolChoice"], {"tool": {"name": "report"}})
        self.assertEqual(request["system"], [{"text": "rules"}])

    def test_regenerates_once_with_the_errors_as_the_tool_result(self):
        client = Scripted(tool_use({"n": "x"}), tool_use({"n": 4}, "t2"))
        usage = bedrock.Usage()
        self.assertEqual(self.call(client, usage=usage), {"n": 4})
        messages = client.requests[1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], [tool_use({"n": "x"})[0][1]])  # the reasoning block is dropped
        result = messages[2]["content"][0]["toolResult"]
        self.assertEqual(result["toolUseId"], "t1")
        self.assertIn("n: not an integer", result["content"][0]["text"])
        self.assertEqual(usage.by_model[bedrock.NEMOTRON], {"inputTokens": 200, "outputTokens": 20, "calls": 2})

    def test_repair_runs_before_validation_and_its_result_is_returned(self):
        client = Scripted(tool_use({"n": "7"}))
        result = bedrock.structured(client, bedrock.NEMOTRON, "rules", "data", TOOL, validate, bedrock.Usage(),
                                    clock(), max_tokens=100, repair=lambda obj: {"n": int(obj["n"])})
        self.assertEqual(result, {"n": 7})
        self.assertEqual(len(client.requests), 1)

    def test_invalid_twice_raises(self):
        client = Scripted(tool_use({"n": "x"}), ([{"text": "no tool"}], "end_turn"))
        with self.assertRaises(bedrock.InvalidOutput) as caught:
            self.call(client)
        self.assertEqual(caught.exception.errors, ["no tool call in the reply"])

    def test_cut_off_reply_counts_as_invalid(self):
        client = Scripted(([{"toolUse": {"toolUseId": "t", "name": "report", "input": {"n": 1}}}], "max_tokens"),
                          tool_use({"n": 1}))
        self.assertEqual(self.call(client), {"n": 1})
        self.assertEqual(len(client.requests), 2)

    def test_json_in_text_for_models_without_tool_use(self):
        client = Scripted(([{"text": 'Sure: {"n": "x"}'}], "end_turn"), ([{"text": '```json\n{"n": 5}\n```'}], "end_turn"))
        self.assertEqual(self.call(client, model=bedrock.GEMMA), {"n": 5})
        first = client.requests[0]
        self.assertNotIn("toolConfig", first)
        self.assertNotIn("system", first)
        self.assertIn("rules", first["messages"][0]["content"][0]["text"])
        self.assertIn(json.dumps(TOOL["schema"]), first["messages"][0]["content"][0]["text"])
        self.assertIn("n: not an integer", client.requests[1]["messages"][2]["content"][0]["text"])


class TestUsageAndClock(unittest.TestCase):
    def test_cost_per_model(self):
        usage = bedrock.Usage()
        usage.add("a", {"inputTokens": 1_000_000, "outputTokens": 500_000})
        usage.add("b", {"inputTokens": 10, "outputTokens": 10})
        self.assertEqual(usage.tokens(), 1_500_020)
        with logs.capture() as records:
            self.assertEqual(usage.cost({"a": {"input": 0.5, "output": 2}}), 1.5)  # b has no price: 0, logged
        self.assertEqual([(r["level"], r["modelId"]) for r in records], [("WARNING", "b")])

    def test_merge_round_trips_through_plain_dicts(self):
        usage = bedrock.Usage({"a": {"inputTokens": 1, "outputTokens": 2, "calls": 1}})
        usage.merge(bedrock.Usage(json.loads(json.dumps(usage.by_model))))
        self.assertEqual(usage.by_model["a"], {"inputTokens": 2, "outputTokens": 4, "calls": 2})

    def test_clock_refuses_a_call_that_might_not_finish(self):
        clock(bedrock.READ_TIMEOUT + 30).require_call()
        with self.assertRaises(bedrock.OutOfTime):
            clock(bedrock.READ_TIMEOUT).require_call()
        clock(100).require_call(seconds=40)


if __name__ == "__main__":
    unittest.main()
