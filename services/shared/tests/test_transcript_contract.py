"""Contract tests for the chunk transcript. Run with `make test-contracts`."""

import json
import unittest

from jsonschema import Draft202012Validator, ValidationError

from contracts import transcripts


def example():
    return json.loads(transcripts.EXAMPLE_PATH.read_text())


class TranscriptContractTest(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(json.loads(transcripts.SCHEMA_PATH.read_text()))

    def test_example_is_valid(self):
        transcripts.validate_transcript(example())

    def test_silent_chunk_is_valid(self):
        transcripts.validate_transcript({**example(), "text": "", "segments": []})

    def assertInvalid(self, body):
        with self.assertRaises(ValidationError):
            transcripts.validate_transcript(body)

    def test_extra_field(self):
        self.assertInvalid({**example(), "userId": "x"})

    def test_missing_segments(self):
        body = example()
        del body["segments"]
        self.assertInvalid(body)

    def test_not_english(self):
        self.assertInvalid({**example(), "language": "ja"})

    def test_empty_segment_text(self):
        self.assertInvalid({**example(), "segments": [{"start": 0, "end": 1, "text": ""}]})


if __name__ == "__main__":
    unittest.main()
