#!/usr/bin/env python3
"""Check the built-in synthesis schemas before they are seeded.

Each file in schemas/builtin/ must:
  - be a valid JSON Schema (draft 2020-12, the draft named in its $schema)
  - have a top-level object type, a title (the seeded `name`) and a description
  - stay inside the portable subset that Bedrock tool input schemas accept: no $ref/$defs,
    every object closed with additionalProperties: false and listing its required keys
  - have no object property inside an object: an object is the top level or a list's item.
    Nemotron flattened a nested object onto the top level in 5 of 6 first attempts
  - say "At most N." in the description of every array with maxItems: descriptions are what
    the model follows, and an unstated limit cost a regeneration

Usage: python3 scripts/check_schemas.py [schemas/builtin]
"""

import json
import pathlib
import sys

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("jsonschema is not installed: pip install jsonschema")

DRAFT = "https://json-schema.org/draft/2020-12/schema"
FORBIDDEN_KEYS = {"$ref", "$defs", "definitions", "$id", "oneOf", "anyOf", "allOf", "not", "if"}
EXPECTED_COUNT = 6


def walk(node, path, problems, parent=None):
    """parent: "object" when node is a property of an object, "array" when it's a list's items."""
    if isinstance(node, dict):
        for key in FORBIDDEN_KEYS & node.keys():
            problems.append(f"{path}: '{key}' is outside the portable subset")
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: object without additionalProperties: false")
            missing = set(node.get("properties", {})) - set(node.get("required", []))
            optional_ok = {"timestamp"}  # the only optional field, by design
            if missing - optional_ok:
                problems.append(f"{path}: properties not in required: {sorted(missing - optional_ok)}")
            if parent == "object":
                problems.append(f"{path}: an object inside an object; make its fields top-level or a list's items")
            for name, prop in node.get("properties", {}).items():
                walk(prop, f"{path}/properties/{name}", problems, "object")
        if node.get("type") == "array":
            if "maxItems" in node and f"At most {node['maxItems']}." not in node.get("description", ""):
                problems.append(f"{path}: maxItems {node['maxItems']} isn't stated as 'At most {node['maxItems']}.' in the description")
            if "items" in node:
                walk(node["items"], f"{path}/items", problems, "array")
        for key, value in node.items():
            if key not in ("properties", "items"):
                walk(value, f"{path}/{key}", problems)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            walk(value, f"{path}/{i}", problems)


def check(file):
    problems = []
    try:
        schema = json.loads(file.read_text())
    except json.JSONDecodeError as e:
        return [f"not valid JSON: {e}"]

    if schema.get("$schema") != DRAFT:
        problems.append(f"$schema must be {DRAFT}")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as e:  # jsonschema.SchemaError
        problems.append(f"not a valid JSON Schema: {e.message}")
    if schema.get("type") != "object":
        problems.append("top level must be type: object")
    for key in ("title", "description"):
        if not schema.get(key):
            problems.append(f"missing top-level {key}")
    walk(schema, "#", problems)
    return problems


def main():
    folder = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "schemas/builtin")
    files = sorted(folder.glob("*.json"))
    failed = False
    for file in files:
        problems = check(file)
        print(f"{'FAIL' if problems else 'ok  '} {file.name}")
        for p in problems:
            print(f"     - {p}")
        failed |= bool(problems)
    if len(files) != EXPECTED_COUNT:
        print(f"expected {EXPECTED_COUNT} schemas, found {len(files)}")
        failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
