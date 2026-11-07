"""synthesise.repair: the slips fixed in code before validation, and that it leaves valid documents alone."""

import json
import pathlib
import unittest

import synthesise

SCHEMAS = pathlib.Path(__file__).resolve().parents[3] / "schemas" / "builtin"
QUOTE = {"type": "object", "properties": {"speaker": {"type": "string"}, "text": {"type": "string"}}}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "themes": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "theme": {"type": "string"},
            "evidence": {"type": "array", "maxItems": 1, "items": QUOTE},
        }}},
        "painPoints": {"type": "array", "items": {"type": "string"}},
        "notableQuotes": {"type": "array", "maxItems": 3, "items": QUOTE},
    },
}


def quote(n):
    return {"speaker": "Participant 1", "text": f"q{n}"}


class TestRepair(unittest.TestCase):
    def test_lists_are_cut_to_max_items_at_every_level(self):
        document = {"themes": [{"theme": t, "evidence": [quote(1), quote(2)]} for t in "abc"],
                    "notableQuotes": [quote(n) for n in range(5)]}
        fixed = synthesise.repair(SCHEMA, document)
        self.assertEqual([t["theme"] for t in fixed["themes"]], ["a", "b"])
        self.assertEqual([t["evidence"] for t in fixed["themes"]], [[quote(1)], [quote(1)]])
        self.assertEqual(fixed["notableQuotes"], [quote(0), quote(1), quote(2)])
        self.assertEqual(len(document["notableQuotes"]), 5)  # the input isn't changed

    def test_a_list_sent_as_a_json_string_is_parsed(self):
        fixed = synthesise.repair(SCHEMA, {"notableQuotes": json.dumps([quote(1)]), "painPoints": '["slow", "costly"]'})
        self.assertEqual(fixed["notableQuotes"], [quote(1)])
        self.assertEqual(fixed["painPoints"], ["slow", "costly"])

    def test_a_list_of_strings_sent_as_lines_is_split(self):
        fixed = synthesise.repair(SCHEMA, {"painPoints": "- slow search\n\n• no edit button\nlate orders"})
        self.assertEqual(fixed["painPoints"], ["slow search", "no edit button", "late orders"])

    def test_what_code_cant_fix_is_left_for_the_validator(self):
        document = {
            "summary": ["not", "a", "string"],
            "painPoints": "one line only",               # one string, no lines: a list of one would be a guess
            "notableQuotes": '[{"speaker": "P1": "text"',  # malformed JSON stays a string
            "themes": '{"theme": "a"}',                   # JSON of the wrong type stays a string
            "extra": 1,
        }
        self.assertEqual(synthesise.repair(SCHEMA, document), document)

    def test_valid_documents_of_every_builtin_template_are_unchanged(self):
        for file in sorted(SCHEMAS.glob("*.json")):
            schema = json.loads(file.read_text())
            document = {name: ("text" if prop["type"] == "string" else []) for name, prop in schema["properties"].items()}
            self.assertEqual(synthesise.repair(schema, document), document, file.name)
            self.assertEqual(synthesise.validator(schema)(document), [], file.name)


if __name__ == "__main__":
    unittest.main()
