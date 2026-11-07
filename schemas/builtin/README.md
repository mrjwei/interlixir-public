# schemas/builtin

The six built-in synthesis schemas: one general default plus one per use case in the requirements doc. Each file is seeded as a `SCHEMA#BUILTIN` item , and synthesis uses it as the forced tool's input schema.

| File (= `schemaId`) | `title` (= seeded `name`) | Use case |
| --- | --- | --- |
| `default.json` | General interview | Any interview; the upload page's default |
| `usability-test.json` | Usability test (think-aloud) | Tasks, outcomes, issues by severity |
| `focus-group.json` | Focus group | Topics with consensus level, agreements, group dynamics |
| `field-visit.json` | Observational field visit | Setting, activities, workarounds, environment |
| `customer-discovery.json` | Customer discovery call | Problems, jobs to be done, willingness to pay, decision process, objections |
| `expert-interview.json` | Expert interview | Insights with confidence, trends, risks |

## Conventions

- **Draft 2020-12** (`$schema` on every file). Python `jsonschema`'s `Draft202012Validator` validates output in M8 and meta-validates user schemas in M19.
- **Portable subset**, because the same document is a Bedrock tool input schema and Nemotron's support for advanced keywords is untested: no `$ref`/`$defs`, no `oneOf`/`anyOf`/`allOf`/`not`/`if`. Shared shapes (the quote object) are inlined instead.
- **Closed objects:** every object has `additionalProperties: false` and lists all its properties in `required` (the only optional field is a quote's `timestamp`). Absent information is an empty array or a phrase like "not discussed", not a missing key, so the renderer never has to guess.
- **No object inside an object.** An object is the top level or a list's items, nothing else. Nemotron flattened a nested object (`customerProfile`, `buyingSignals`, `expertProfile`) onto the top level in 5 of 6 first attempts, so those became a string or top-level fields.
- **Limits are stated.** An array with `maxItems` ends its description with "At most N.", and lists are asked for most important first. The model still sometimes writes one more, so synthesis cuts a list to its `maxItems` before validating (`synthesise.repair`) instead of spending the regeneration on it; the order means the cut drops the least important.
- **`description` on every field.** They are the model's instructions for that field, so write them as instructions.
- **Property order is meaningful:** the model tends to fill fields in the order given, so `summary` comes first. The seeded item stores the raw file text (not a re-encoded copy, which could reorder the keys).
- Quotes are pseudonymised (`speaker` = "Participant 1") because they come from the redacted transcript.
- Synthesis strips `$schema` before passing a schema to Bedrock as a tool schema (`synthesise.tool_schema`).

## Checking

```bash
python3 scripts/check_schemas.py    # or: make check-schemas
```

It meta-validates each file against draft 2020-12 and enforces the conventions above.

```bash
make seed-schemas                     # dry run: which items would change
make seed-schemas ARGS=--apply        # write them (check-schemas runs first)
```
