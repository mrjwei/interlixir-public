"""Pseudonym assignment and replacement (redaction.pseudonymise), windowing, and the regex backstop."""

import unittest

import bedrock
import redaction


def segments(*texts):
    return [{"start": float(i), "end": float(i) + 1, "text": t} for i, t in enumerate(texts)]


def person(text, refers_to=None):
    return {"text": text, "type": "person", "refersTo": refers_to or text}


class TestPseudonymise(unittest.TestCase):
    def test_short_and_full_names_share_one_pseudonym(self):
        out, counts = redaction.pseudonymise(
            segments("I'm Sarah Chen.", "Sarah said no.", "Ms Chen agreed."),
            [person("Sarah Chen"), person("Sarah", "Sarah"), person("Chen", "Chen")],
        )
        self.assertEqual([s["text"] for s in out], ["I'm [Person 1].", "[Person 1] said no.", "Ms [Person 1] agreed."])
        self.assertEqual(counts, {"person": 1, "unmatched": 0})

    def test_numbered_by_first_appearance_and_case_insensitive(self):
        out, counts = redaction.pseudonymise(
            segments("TOM met Anna", "ANNA and Tom"),
            [person("Anna"), person("Tom")],
        )
        self.assertEqual([s["text"] for s in out], ["[Person 1] met [Person 2]", "[Person 2] and [Person 1]"])
        self.assertEqual(counts["person"], 2)

    def test_a_capitalised_name_leaves_the_lower_case_word(self):
        out, counts = redaction.pseudonymise(
            segments("Rose set it up.", "Bookings rose after that.", "Ask will about it, Will said."),
            [person("Rose"), person("will")],
        )
        # "will" was reported in lower case, so it was written that way and every form goes.
        self.assertEqual([s["text"] for s in out],
                         ["[Person 1] set it up.", "Bookings rose after that.", "Ask [Person 2] about it, [Person 2] said."])
        self.assertEqual(counts["person"], 2)

    def test_standalone_first_and_last_names_of_a_full_name(self):
        out, counts = redaction.pseudonymise(
            segments("I'm Sarah Mitchell.", "And Sarah, how many?", "Mitchell's team grew.", "sarah is fine."),
            [person("Sarah Mitchell")],
        )
        self.assertEqual([s["text"] for s in out],
                         ["I'm [Person 1].", "And [Person 1], how many?", "[Person 1]'s team grew.", "sarah is fine."])
        self.assertEqual(counts, {"person": 1, "unmatched": 0, "shortForms": 2})

    def test_short_forms_skip_titles_and_short_words(self):
        out, counts = redaction.pseudonymise(
            segments("Mrs Patel met Al Smith.", "Mrs Jones and Al came later."),
            [person("Mrs Patel"), person("Al Smith")],
        )
        self.assertEqual([s["text"] for s in out], ["[Person 1] met [Person 2].", "Mrs Jones and Al came later."])
        self.assertNotIn("shortForms", counts)

    def test_an_ambiguous_short_form_gets_its_own_pseudonym(self):
        out, counts = redaction.pseudonymise(
            segments("Sarah Chen and Sarah Mitchell both came.", "Sarah spoke first."),
            [person("Sarah Chen"), person("Sarah Mitchell")],
        )
        self.assertEqual([s["text"] for s in out], ["[Person 1] and [Person 2] both came.", "[Person 3] spoke first."])
        self.assertEqual(counts["shortForms"], 1)

    def test_replaced_spans_match_what_is_replaced(self):
        text = "Rose met Tom.\nBookings rose."
        self.assertEqual(redaction.replaced_spans(text, [person("Rose"), person("Tom")]),
                         [(0, 4, "person"), (9, 12, "person")])
        self.assertEqual(redaction.replaced_spans(text, []), [])

    def test_organisations_are_lettered_and_types_kept_apart(self):
        out, counts = redaction.pseudonymise(
            segments("I work at Acme and Globex, Jordan runs Acme."),
            [{"text": "Acme", "type": "organisation", "refersTo": "Acme"},
             {"text": "Globex", "type": "organisation", "refersTo": "Globex"}, person("Jordan")],
        )
        self.assertEqual(out[0]["text"], "I work at [Organisation A] and [Organisation B], [Person 1] runs [Organisation A].")
        self.assertEqual(counts, {"organisation": 2, "person": 1, "unmatched": 0})

    def test_whole_words_only(self):
        out, _ = redaction.pseudonymise(segments("Al said it's all fine."), [person("Al")])
        self.assertEqual(out[0]["text"], "[Person 1] said it's all fine.")

    def test_entities_not_in_the_text_or_of_unknown_type_are_dropped_and_counted(self):
        out, counts = redaction.pseudonymise(
            segments("Nothing to see."),
            [person("Bob"), {"text": "see", "type": "colour", "refersTo": "see"}, person("")],
        )
        self.assertEqual(out[0]["text"], "Nothing to see.")
        self.assertEqual(counts, {"unmatched": 3})

    def test_ambiguous_first_name_is_not_merged(self):
        out, _ = redaction.pseudonymise(
            segments("Sam Lee and Sam Ng and Sam"),
            [person("Sam Lee"), person("Sam Ng"), person("Sam", "Sam")],
        )
        self.assertEqual(out[0]["text"], "[Person 1] and [Person 2] and [Person 3]")

    def test_input_is_not_modified(self):
        original = segments("Hi Kim")
        redaction.pseudonymise(original, [person("Kim")])
        self.assertEqual(original[0]["text"], "Hi Kim")


class FakeClient:
    def __init__(self, entities=()):
        self.entities, self.prompts = list(entities), []

    def converse(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"][0]["text"])
        return {"output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t", "name": "report_pii", "input": {"entities": self.entities}}}]}},
            "stopReason": "tool_use", "usage": {"inputTokens": 10, "outputTokens": 2}}


class Clock:
    def require_call(self, seconds=None):
        pass


class TestFindEntities(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(redaction.windows(["aaaa", "bbbb", "cccc"], 10), [["aaaa", "bbbb"], ["cccc"]])
        self.assertEqual(redaction.windows(["x" * 50, "y"], 10), [["x" * 50], ["y"]])
        self.assertEqual(redaction.windows([], 10), [])

    def test_one_call_per_window_plus_the_regex_backstop(self):
        client, usage = FakeClient(), bedrock.Usage()
        lines = ["[00:01] mail me at kim.lee@example.com.au", "[00:05] or ring 0412 345 678",
                 "[00:09] my TFN is 123 456 782", "[00:12] it was in 2019, 2020",
                 "[00:15] call me on 0412, 345, 679", "[00:18] office +61 7 3138 2000.",
                 "[00:21] front desk (07) 3856 4410"]
        found = redaction.find_entities(client, bedrock.NEMOTRON, lines, usage, Clock(), 40, 2)
        self.assertEqual(len(client.prompts), 7)
        self.assertEqual(usage.by_model[bedrock.NEMOTRON]["calls"], 7)
        self.assertEqual(
            [(e["type"], e["text"]) for e in found],
            [("email", "kim.lee@example.com.au"), ("phone", "0412 345 678"), ("phone", "0412, 345, 679"),
             ("phone", "+61 7 3138 2000"), ("phone", "(07) 3856 4410"), ("id_number", "123 456 782")],
        )
        self.assertTrue(client.prompts[0].startswith("<transcript_excerpt>\n[00:01]"))


if __name__ == "__main__":
    unittest.main()
