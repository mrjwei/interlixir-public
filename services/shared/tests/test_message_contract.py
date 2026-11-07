"""Contract tests for the A3 queue messages. Run with `make test-contracts`."""

import json
import pathlib
import unittest

from jsonschema import Draft202012Validator, ValidationError

from contracts import messages

EXAMPLES = pathlib.Path(messages.__file__).with_name("message_examples")


def example(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


class SchemaTest(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(json.loads(messages.SCHEMA_PATH.read_text()))

    def test_schema_and_module_agree(self):
        schema = json.loads(messages.SCHEMA_PATH.read_text())
        self.assertTrue(set(messages.MESSAGE_TYPES) <= schema["$defs"].keys())

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            messages.validate_message("Nope", {})


class ExamplesTest(unittest.TestCase):
    def test_one_valid_example_per_type(self):
        self.assertEqual({f.stem for f in EXAMPLES.glob("*.json")}, set(messages.MESSAGE_TYPES))
        for name in messages.MESSAGE_TYPES:
            with self.subTest(name):
                messages.validate_message(name, example(name))


class RejectsTest(unittest.TestCase):
    def assertInvalid(self, name, body):
        with self.assertRaises(ValidationError):
            messages.validate_message(name, body)

    def test_extra_field(self):
        self.assertInvalid("TranscriptionChunk", {**example("TranscriptionChunk"), "bucket": "x"})

    def test_missing_chunks_total(self):
        body = example("TranscriptionChunk")
        del body["chunksTotal"]
        self.assertInvalid("TranscriptionChunk", body)

    def test_negative_chunk_and_zero_total(self):
        self.assertInvalid("TranscriptionChunk", {**example("TranscriptionChunk"), "chunk": -1})
        self.assertInvalid("TranscriptionChunk", {**example("TranscriptionChunk"), "chunksTotal": 0})

    def test_chunk_number_as_string(self):
        self.assertInvalid("TranscriptionChunk", {**example("TranscriptionChunk"), "chunk": "2"})

    def test_id_with_key_delimiter(self):
        self.assertInvalid("SynthesisRequest", {**example("SynthesisRequest"), "jobId": "a/b"})
        self.assertInvalid("SynthesisRequest", {**example("SynthesisRequest"), "userId": "USER#x"})


if __name__ == "__main__":
    unittest.main()
