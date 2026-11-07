"""Bedrock Converse calls for synthesis: in-region, by raw model ID (the account SCP denies
application inference profiles, and these models have none).

Two ways to get structured output, picked by model:
- Nemotron has native tool use and honours a forced toolChoice: the JSON Schema is the
  tool's input schema and the tool call's input is the result.
- Gemma has no tool use in Converse (the shared platform finding): the schema goes into the prompt and the reply is parsed as
  JSON. Its support for a system prompt is untested, so the instructions go into the user message too.
Either way the caller's validator checks the result, and an invalid one is sent back once with the errors
.

Every call adds its tokens to a Usage, per model, so cost can be priced per model (the prices come from
Parameter Store). Every call is also metered as it happens (LlmTokens and LlmCost by Model), so the
dashboard counts calls whose result was lost too: a crashed run, a regeneration, a failed job.
"""

import json
import os
import threading

from contracts import logs, prices as price_table

NEMOTRON = "nvidia.nemotron-super-3-120b"
GEMMA = "google.gemma-3-12b-it"
# Models with native tool use in Converse; the others get the JSON-in-text path.
TOOL_USE_MODELS = {NEMOTRON}

# A call's longest wait for a response. Converse isn't streamed, so nothing arrives until the model is done:
# this bounds one call (per attempt) and must fit in what's left of the function's timeout (see Clock).
READ_TIMEOUT = int(os.environ.get("BEDROCK_READ_TIMEOUT", "300"))
# Left after a call for the writes that follow it.
AFTER_CALL_SECONDS = 20


class OutOfTime(Exception):
    """Not enough of the invocation is left for another call. The work so far is saved, so the message is
    released and the next invocation carries on from there."""


class InvalidOutput(Exception):
    """Still invalid after the one regeneration. The input is the likely cause, so retrying won't help."""

    def __init__(self, errors):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


class Clock:
    """Seconds left in this invocation (the Lambda context), checked before every call."""

    def __init__(self, context=None):
        self.context = context

    def left(self):
        return self.context.get_remaining_time_in_millis() / 1000 if self.context else 900

    def require_call(self, seconds=None):
        """seconds: how long this call may take (default: the read timeout, the longest a call can wait)."""
        need = (seconds or READ_TIMEOUT) + AFTER_CALL_SECONDS
        if self.left() < need:
            raise OutOfTime(f"{self.left():.0f} s left, the call needs {need} s")


class Usage:
    """Tokens per model: {modelId: {"inputTokens": n, "outputTokens": n, "calls": n}}. Plain dicts, so it can
    be stored with the step's output and added up again on a later invocation. Redaction windows run in threads,
    so adding is locked."""

    def __init__(self, by_model=None):
        self.by_model = {m: dict(v) for m, v in (by_model or {}).items()}
        self._lock = threading.Lock()

    def add(self, model_id, usage):
        with self._lock:
            entry = self.by_model.setdefault(model_id, {"inputTokens": 0, "outputTokens": 0, "calls": 0})
            entry["inputTokens"] += int(usage.get("inputTokens", 0))
            entry["outputTokens"] += int(usage.get("outputTokens", 0))
            entry["calls"] += 1

    def merge(self, other):
        for model_id, entry in other.by_model.items():
            mine = self.by_model.setdefault(model_id, {"inputTokens": 0, "outputTokens": 0, "calls": 0})
            for name in mine:
                mine[name] += int(entry.get(name, 0))
        return self

    def tokens(self):
        return sum(e["inputTokens"] + e["outputTokens"] for e in self.by_model.values())

    def cost(self, prices):
        """USD. prices: {modelId: {"input": usd_per_million, "output": usd_per_million}}. A model with no price
        counts as 0 and is logged, so a missing price shows up instead of silently under-reporting."""
        total = 0.0
        for model_id, entry in self.by_model.items():
            price = prices.get(model_id)
            if price is None:
                logs.warning("no price for model, cost counted as 0", modelId=model_id)
                continue
            total += entry["inputTokens"] * float(price["input"]) / 1e6
            total += entry["outputTokens"] * float(price["output"]) / 1e6
        return round(total, 6)


def prices():
    """{modelId: {"input": usd, "output": usd}} per million tokens, read from Parameter Store (cached;
    contracts/prices.py)."""
    return price_table.get()


# --- calls -------------------------------------------------------------------------------------------


def structured(client, model_id, system, prompt, tool, validate, usage, clock, max_tokens, temperature=0.0,
               repair=None):
    """One JSON object that passes validate(obj) -> [error strings]. tool: {"name", "description", "schema"}.
    repair(obj) -> obj, if given, fixes what code can fix safely before validating, so a regeneration is spent
    only on what it can't. Two attempts: the second sees the first answer and its errors. Raises InvalidOutput
    if both fail."""
    if model_id in TOOL_USE_MODELS:
        messages = [_user(prompt)]
        config = {
            "tools": [{"toolSpec": {
                "name": tool["name"], "description": tool["description"], "inputSchema": {"json": tool["schema"]},
            }}],
            "toolChoice": {"tool": {"name": tool["name"]}},
        }
    else:
        messages = [_user(
            f"{system}\n\n{prompt}\n\nReply with only one JSON object, with no other text before or after it. "
            f"It must match this JSON Schema:\n{json.dumps(tool['schema'])}"
        )]
        system, config = None, None

    errors = []
    for attempt in (1, 2):
        clock.require_call()
        response = _converse(client, model_id, system, messages, max_tokens, temperature, config)
        usage.add(model_id, response.get("usage", {}))
        message = _echo(response["output"]["message"])
        obj, tool_use_id, errors = _parse(message, config is not None)
        if response.get("stopReason") == "max_tokens":
            errors = errors + [f"the reply was cut off at {max_tokens} output tokens; be more concise"]
        if not errors:
            obj = repair(obj) if repair else obj
            errors = validate(obj)
        _metered("structured call", model_id, response, tool=tool["name"], attempt=attempt, errors=len(errors))
        if not errors:
            return obj
        feedback = (
            "That output is not valid:\n- " + "\n- ".join(errors[:20])
            + f"\nFix every problem and {'call ' + tool['name'] + ' again' if config else 'reply with the corrected JSON object only'}."
        )
        messages = messages + [message, _tool_result(tool_use_id, feedback) if tool_use_id else _user(feedback)]
    raise InvalidOutput(errors)


def text(client, model_id, prompt, usage, clock, max_tokens, temperature=0.3, seconds=None):
    """A plain text reply (the title). The prompt carries any instructions, so it works for every model."""
    clock.require_call(seconds)
    response = _converse(client, model_id, None, [_user(prompt)], max_tokens, temperature, None)
    usage.add(model_id, response.get("usage", {}))
    _metered("text call", model_id, response)
    return "".join(block.get("text", "") for block in response["output"]["message"]["content"])


def _converse(client, model_id, system, messages, max_tokens, temperature, tool_config):
    kwargs = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    if tool_config:
        kwargs["toolConfig"] = tool_config
    return client.converse(**kwargs)


def _parse(message, tool_use):
    """(obj, toolUseId, errors) from an assistant message."""
    if tool_use:
        for block in message["content"]:
            if "toolUse" in block:
                value = block["toolUse"].get("input")
                if not isinstance(value, dict):
                    return None, block["toolUse"]["toolUseId"], ["the tool input is not a JSON object"]
                return value, block["toolUse"]["toolUseId"], []
        return None, None, ["no tool call in the reply"]
    reply = "".join(block.get("text", "") for block in message["content"])
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        return None, None, ["no JSON object in the reply"]
    try:
        value = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as error:
        return None, None, [f"the JSON doesn't parse: {error}"]
    if not isinstance(value, dict):
        return None, None, ["the reply is not a JSON object"]
    return value, None, []


def _echo(message):
    """The assistant message as it can be sent back: text and tool calls only (a model may add reasoning
    blocks, which aren't needed for the second attempt)."""
    content = [block for block in message.get("content", []) if "text" in block or "toolUse" in block]
    return {"role": "assistant", "content": content or [{"text": "(empty reply)"}]}


def _user(prompt):
    return {"role": "user", "content": [{"text": prompt}]}


def _tool_result(tool_use_id, feedback):
    # No "status": Converse only accepts it for some model families.
    return {"role": "user", "content": [{"toolResult": {"toolUseId": tool_use_id, "content": [{"text": feedback}]}}]}


def _log(msg, **fields):
    logs.info(msg, **fields)


def _metered(msg, model_id, response, **fields):
    """One line per call, which is also the LlmTokens / LlmCost (USD) metrics by Model and in total."""
    tokens = response.get("usage", {})
    price = prices().get(model_id)
    cost = 0.0
    if price is not None:
        cost = (int(tokens.get("inputTokens", 0)) * float(price["input"])
                + int(tokens.get("outputTokens", 0)) * float(price["output"])) / 1e6
    logs.metric(
        "INFO", msg,
        {"LlmTokens": (int(tokens.get("inputTokens", 0)) + int(tokens.get("outputTokens", 0)), "Count"),
         "LlmCost": (round(cost, 6), "None")},
        {"Model": model_id},
        modelId=model_id, stopReason=response.get("stopReason"), usage=tokens,
        durationMs=response.get("metrics", {}).get("latencyMs"), **fields,
    )
