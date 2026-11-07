"""Queue message contract for the A3 pipeline. The schema is messages.schema.json beside this file.

Like events.py, nothing here runs in a Lambda: producers build plain dicts, and their unit tests check them
with validate_message().
"""

import json
import pathlib

MESSAGE_TYPES = ("TranscriptionChunk", "SynthesisRequest")

SCHEMA_PATH = pathlib.Path(__file__).with_name("messages.schema.json")


def validate_message(name, body):
    """Validate one message body (a dict) against $defs/<name>. Raises ValidationError. Needs jsonschema."""
    from jsonschema import Draft202012Validator

    if name not in MESSAGE_TYPES:
        raise ValueError(f"unknown message type: {name}")
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"}).validate(body)
