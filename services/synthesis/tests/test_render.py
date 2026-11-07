"""Markdown rendering driven by the schema: every built-in template renders, and the layout rules hold."""

import json
import pathlib
import unittest

import render

BUILTIN = pathlib.Path(__file__).resolve().parents[3] / "schemas" / "builtin"


def sample(schema):
    """A minimal valid-shaped value for a schema: one item per array, the first enum value."""
    kind = schema.get("type")
    if kind == "object":
        return {name: sample(prop) for name, prop in schema.get("properties", {}).items()}
    if kind == "array":
        return [sample(schema.get("items", {}))]
    if "enum" in schema:
        return schema["enum"][0]
    if "pattern" in schema:
        return "01:02"
    return {"string": "text", "integer": 1, "number": 1.5, "boolean": True}.get(kind, "text")


class TestRender(unittest.TestCase):
    def test_every_builtin_template_renders_each_section(self):
        for path in sorted(BUILTIN.glob("*.json")):
            schema = json.loads(path.read_text())
            with self.subTest(path.name):
                markdown = render.render("Title", schema, sample(schema), [("Template", schema["title"])])
                self.assertTrue(markdown.startswith("# Title\n"))
                for name, prop in schema["properties"].items():
                    self.assertIn(f"\n## {render._label(name, prop)}\n", markdown)
                self.assertIn("- **Template:** " + schema["title"], markdown)

    def test_layout(self):
        schema = json.loads((BUILTIN / "usability-test.json").read_text())
        doc = {
            "summary": "Line one\nline two",
            "participantProfile": "# not a heading",
            "tasks": [{
                "task": "Change the address",
                "outcome": "completed_with_difficulty",
                "observations": "Looked for search.",
                "evidence": [{"speaker": "Participant 1", "text": "where is it", "timestamp": "05:01"}],
            }],
            "issues": [],
            "positives": ["Fast checkout"],
            "notableQuotes": [{"speaker": "Participant 1", "text": "> nice"}],
        }
        markdown = render.render("T", schema, doc, [("Instruction", None), ("Template", "Usability")])
        self.assertIn("## Summary\n\nLine one line two\n", markdown)
        self.assertIn("## Participant profile\n\n\\# not a heading\n", markdown)
        self.assertIn("### Change the address\n\n**Outcome:** Completed with difficulty\n", markdown)
        self.assertIn("**Evidence:**\n\n> “where is it”\n> — Participant 1, 05:01\n", markdown)
        self.assertIn("## Issues\n\n_None._\n", markdown)
        self.assertIn("## Positives\n\n- Fast checkout\n", markdown)
        self.assertIn("> “\\> nice”\n> — Participant 1\n", markdown)
        self.assertNotIn("Instruction", markdown)  # empty "about" values are left out

    def test_humanise(self):
        self.assertEqual(render._humanise("painPoints"), "Pain points")
        self.assertEqual(render._humanise("completed_with_difficulty"), "Completed with difficulty")
        self.assertEqual(render._humanise("jobsToBeDone"), "Jobs to be done")


if __name__ == "__main__":
    unittest.main()
