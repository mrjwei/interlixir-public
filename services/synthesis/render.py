"""Markdown from a validated synthesis result, driven by its JSON Schema.

Nothing here knows the six built-in templates: the schema's property order gives the section order, its
titles or property names the headings, and its shapes the layout. That way custom schemas render with
no new code. Layout rules:
- top-level property -> "## Heading"; a string is a paragraph, a list of strings is bullets
- a list of quotes (objects with "speaker" and "text") -> block quotes with the speaker and timestamp
- a list of other objects -> one "###" sub-section per item, headed by its first string field; the other
  fields follow as "**Label:** value" lines, and nested lists as bullets or quotes
- enum values like "completed_with_difficulty" read as "Completed with difficulty"
Every value comes from the model, so it's flattened to one line where a line is expected; the SPA must still
render Markdown without raw HTML.
"""

import re


def render(title, schema, document, about):
    """about: [(label, value)] for the closing "About this document" section."""
    out = [f"# {_line(title)}", ""]
    for name, prop in schema.get("properties", {}).items():
        out += [f"## {_label(name, prop)}", ""]
        out += _block(document.get(name), prop, level=3)
        out.append("")
    out += ["---", "", "## About this document", ""]
    out += [f"- **{label}:** {_line(value)}" for label, value in about if value not in (None, "")]
    return "\n".join(out).rstrip() + "\n"


def _block(value, prop, level):
    if value is None or value == [] or value == "":
        return ["_None._"]
    kind = prop.get("type")
    if kind == "array":
        items = prop.get("items", {})
        if _is_quote(items):
            return _quotes(value)
        if items.get("type") == "object":
            return _objects(value, items, level)
        return [f"- {_scalar(v, items)}" for v in value]
    if kind == "object":
        return _fields(value, prop, level)
    return [_scalar(value, prop)]


def _objects(values, items, level):
    props = items.get("properties", {})
    lead = next((n for n, p in props.items() if p.get("type") == "string" and "enum" not in p), None)
    out = []
    for value in values:
        heading = _line(value.get(lead)) if lead else ""
        out += [f"{'#' * min(level, 6)} {heading or '-'}", ""]
        out += _fields(value, items, level + 1, skip=lead)
        out.append("")
    return out[:-1]


def _fields(value, prop, level, skip=None):
    out = []
    for name, sub in prop.get("properties", {}).items():
        if name == skip or name not in value:
            continue
        label = _label(name, sub)
        if sub.get("type") in ("array", "object"):
            out += [f"**{label}:**", ""] + _block(value[name], sub, level) + [""]
        else:
            out += [f"**{label}:** {_scalar(value[name], sub)}", ""]
    return out[:-1] if out else []


def _quotes(values):
    out = []
    for quote in values:
        source = _line(quote.get("speaker")) or "Unknown speaker"
        if quote.get("timestamp"):
            source += f", {_line(quote['timestamp'])}"
        out += [f"> “{_line(quote.get('text'))}”", f"> — {source}", ""]
    return out[:-1]


def _is_quote(items):
    props = items.get("properties", {})
    return items.get("type") == "object" and "speaker" in props and "text" in props


def _scalar(value, prop):
    if "enum" in prop and isinstance(value, str):
        return _humanise(value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return _line(value)


def _label(name, prop):
    return _line(prop.get("title")) or _humanise(name)


def _humanise(name):
    """painPoints -> Pain points, completed_with_difficulty -> Completed with difficulty."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name)).replace("_", " ").split()
    text = " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in words)
    return text[:1].upper() + text[1:]


def _line(value):
    """One line of text: model output can contain newlines, and a leading '#' or '>' would start a heading or
    quote."""
    text = " ".join(str(value if value is not None else "").split())
    return "\\" + text if text[:1] in ("#", ">") else text
