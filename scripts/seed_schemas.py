#!/usr/bin/env python3
"""Re-seed the 6 built-in schema items from schemas/builtin/.

Each item: PK = SCHEMA#BUILTIN, SK = SCHEMA#<schemaId>, schemaId (the file name), name (the schema's title),
description (its description), definition (the raw file text, byte for byte: the property order is part of
the prompt, so it's never re-serialised).

Without --apply it only reads (GetItem, Query) and prints what would change: new, changed (which attributes),
or unchanged. With --apply it writes the new and changed items with PutItem, and nothing else. A SCHEMA#BUILTIN
item with no file is reported, never deleted. Run `make check-schemas` first (the make target does).

Synthesis reads the definition at run time and the API doesn't cache schemas, so the next job uses the new
version; jobs already past synthesis keep the document they have.

Usage (synthesis venv, for boto3):
  make seed-schemas               # dry run
  make seed-schemas ARGS=--apply
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FOLDER = ROOT / "schemas" / "builtin"
TABLE = "interlixir-data"
REGION = "ap-southeast-2"
PK = "SCHEMA#BUILTIN"
EXPECTED_COUNT = 6


def items():
    out = []
    for file in sorted(FOLDER.glob("*.json")):
        text = file.read_text()
        schema = json.loads(text)
        out.append({
            "PK": PK, "SK": f"SCHEMA#{file.stem}", "schemaId": file.stem,
            "name": schema["title"], "description": schema["description"], "definition": text,
        })
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the new and changed items")
    args = parser.parse_args()

    wanted = items()
    if len(wanted) != EXPECTED_COUNT:
        sys.exit(f"expected {EXPECTED_COUNT} files in {FOLDER}, found {len(wanted)}")

    import boto3

    client = boto3.client("dynamodb", region_name=REGION)
    current = {}
    for page in client.get_paginator("query").paginate(
        TableName=TABLE, KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": {"S": PK}},
    ):
        for item in page["Items"]:
            current[item["SK"]["S"]] = {k: v.get("S") for k, v in item.items()}

    to_write = []
    for item in wanted:
        have = current.get(item["SK"])
        if have is None:
            print(f"new        {item['schemaId']}")
            to_write.append(item)
            continue
        changed = [k for k in item if have.get(k) != item[k]]
        extra = sorted(set(have) - set(item))
        if changed:
            detail = ", ".join(
                f"definition {len(have.get('definition') or '')} -> {len(item['definition'])} chars" if k == "definition"
                else k for k in changed
            )
            print(f"changed    {item['schemaId']}: {detail}")
            to_write.append(item)
        else:
            print(f"unchanged  {item['schemaId']}")
        if extra:
            print(f"           (PutItem would drop attributes not in the layout: {', '.join(extra)})")
    for sk in sorted(set(current) - {i["SK"] for i in wanted}):
        print(f"no file    {sk} (left as it is)")

    if not to_write:
        print("nothing to write")
        return
    if not args.apply:
        print(f"dry run: {len(to_write)} item(s) would be written; run with --apply to write them")
        return
    for item in to_write:
        client.put_item(TableName=TABLE, Item={k: {"S": v} for k, v in item.items()})
        print(f"wrote      {item['SK']}")
    # Read back: the stored definition must be the file's text, byte for byte.
    for item in to_write:
        stored = client.get_item(TableName=TABLE, Key={"PK": {"S": PK}, "SK": {"S": item["SK"]}}, ConsistentRead=True)
        ok = stored.get("Item", {}).get("definition", {}).get("S") == item["definition"]
        print(f"{'ok' if ok else 'MISMATCH':<10} {item['SK']} read back")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
