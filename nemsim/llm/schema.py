"""Parse a model's JSON and validate it against a small JSON-Schema subset.

CI's lock has no jsonschema, so this checks the keywords extraction schemas
need and nothing more: type, required, properties, additionalProperties,
enum, items, minItems, maxItems, minLength, maxLength, minimum, maximum.
An unsupported keyword is an error, so a schema never silently checks less
than it appears to.

parse_json rejects NaN and Infinity (Python's json accepts them by default).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

SUPPORTED = {"type", "required", "properties", "additionalProperties", "enum", "items",
             "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "description"}

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


class SchemaError(ValueError):
    """The response is not valid against the schema. Never data."""


def _reject_constant(name: str) -> Any:
    raise SchemaError(f"non-finite number {name} in JSON")


def parse_json(text: str) -> Any:
    """The JSON object in a model's reply. Strips <think> blocks and one
    ```json fence; anything else around the object is a failure, not
    something to guess around."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    if not text:
        raise SchemaError("empty response")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"not JSON: {exc.msg} at char {exc.pos}") from None


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return isinstance(value, _TYPES[name])


def validate(value: Any, schema: dict, path: str = "$") -> None:
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"{path}: schema uses unsupported keywords {sorted(unknown)}")

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in types):
            raise SchemaError(f"{path}: expected {'/'.join(types)}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not one of {schema['enum']}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise SchemaError(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path}: longer than {schema['maxLength']}")

    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: above {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing required {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties", True) is False:
            extra = set(value) - set(properties)
            if extra:
                raise SchemaError(f"{path}: unexpected keys {sorted(extra)}")
        for key, sub in properties.items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise SchemaError(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{i}]")
