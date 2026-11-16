# -*- coding: utf-8 -*-
"""Check an answer against the small JSON Schema subset the stockyt task schemas use.

`claude -p --json-schema` enforces the schema itself, but a session agent's answer file
does not, so every answer goes through this check. Supported keywords: type (one or a
list), enum, properties, required, additionalProperties: false, items.
"""

from __future__ import annotations

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def schema_errors(value, schema: dict, where: str = "answer") -> list[str]:
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_TYPES[t](value) for t in types):
            return [f"{where}: expected {' or '.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where}: {value!r} is not one of {schema['enum']}"]

    errors: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        errors += [f"{where}: missing {key!r}" for key in schema.get("required", []) if key not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{where}: unexpected {key!r}" for key in value if key not in properties]
        for key, sub in properties.items():
            if key in value:
                errors += schema_errors(value[key], sub, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{where}[{i}]")
    return errors
