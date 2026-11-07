"""Chunk transcript contract. The schema is transcripts.schema.json beside this file.

Like messages.py, nothing here runs in the worker: it builds a plain dict, and its unit tests check it with
validate_transcript().
"""

import json
import pathlib

SCHEMA_PATH = pathlib.Path(__file__).with_name("transcripts.schema.json")
EXAMPLE_PATH = pathlib.Path(__file__).with_name("transcript_example.json")


def validate_transcript(body):
    """Validate one chunk transcript (a dict). Raises ValidationError. Needs jsonschema."""
    from jsonschema import Draft202012Validator

    Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).validate(body)
