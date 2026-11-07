"""The model price table: one Parameter Store parameter prices every Bedrock call, in
synthesis and in the chat agent, so a price change needs no redeploy.

The parameter is a String holding JSON: {modelId: {"input": usd, "output": usd}}, USD per million tokens, from the
Bedrock pricing page (ap-southeast-2, on-demand). Name: MODEL_PRICES_PARAMETER, default /interlixir/model-prices.

- **Cached** for TTL_SECONDS per process, so a change shows up on the next job after at most a minute and a
  burst of calls costs one read.
- **Bounded:** connect 2 s, read 3 s, 2 attempts. A slow SSM can hold one call up for a few seconds, never a minute.
- **Never fatal:** if the read fails or the value isn't a valid table, the last good table is kept; with none, the
  MODEL_PRICES environment variable (the pre-M21 setting) is used; with none of that, the table is empty and
  cost is 0, which the callers log per model. The failure is logged and the next try is a TTL later.
- MODEL_PRICES_PARAMETER set to an empty string turns SSM off and reads only MODEL_PRICES (tests, local runs).
"""

import json
import os
import threading
import time

from contracts import logs

DEFAULT_PARAMETER = "/interlixir/model-prices"
TTL_SECONDS = 60

_lock = threading.Lock()
_state = {"table": None, "fetched": 0.0}
_client = None


def get():
    """The price table: {modelId: {"input": usd_per_million, "output": usd_per_million}}."""
    name = os.environ.get("MODEL_PRICES_PARAMETER", DEFAULT_PARAMETER)
    if not name:
        return _from_environment()
    with _lock:
        now = time.monotonic()
        if _state["table"] is None or now - _state["fetched"] >= TTL_SECONDS:
            _state["fetched"] = now
            try:
                _state["table"] = _check(json.loads(_ssm().get_parameter(Name=name)["Parameter"]["Value"]))
            except Exception as error:
                logs.warning("model price table not read from Parameter Store", parameter=name, error=repr(error),
                             using="last read" if _state["table"] is not None else "MODEL_PRICES")
                if _state["table"] is None:
                    _state["table"] = _from_environment()
        return _state["table"]


def reset():
    """Forget the cached table (tests)."""
    with _lock:
        _state.update(table=None, fetched=0.0)


def _check(table):
    if not isinstance(table, dict):
        raise ValueError("the price table is not a JSON object")
    for model_id, price in table.items():
        float(price["input"]), float(price["output"])
    return table


def _from_environment():
    try:
        return _check(json.loads(os.environ.get("MODEL_PRICES") or "{}"))
    except Exception as error:
        logs.warning("MODEL_PRICES is not a valid price table", error=repr(error))
        return {}


def _ssm():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "ssm", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"),
            config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 2}),
        )
    return _client
