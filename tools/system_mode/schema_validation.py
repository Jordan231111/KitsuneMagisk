"""Dependency-free validator for the checked-in System Mode JSON schemas.

The project intentionally avoids downloading Python packages in product gates.
This implements only the JSON Schema 2020-12 keywords used by the checked-in
contracts, while resolving every local ``$ref`` from the schema itself.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Mapping


class SchemaValidationError(ValueError):
    """Raised when an instance violates a checked-in contract."""


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int aliasing."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return type(left) is type(right) and left == right


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, Mapping),
    }.get(expected, False)


def _resolve_pointer(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"unsupported non-local schema reference: {reference}")
    current: Any = root
    for raw in reference[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or key not in current:
            raise SchemaValidationError(f"invalid schema reference: {reference}")
        current = current[key]
    if not isinstance(current, Mapping):
        raise SchemaValidationError(f"schema reference is not an object: {reference}")
    return current


def validate_schema_instance(
    instance: Any,
    schema: Mapping[str, Any],
    *,
    root_schema: Mapping[str, Any] | None = None,
    path: str = "$",
) -> None:
    """Validate ``instance`` against the supported checked-in schema subset."""

    root = schema if root_schema is None else root_schema
    if "$ref" in schema:
        validate_schema_instance(
            instance,
            _resolve_pointer(root, str(schema["$ref"])),
            root_schema=root,
            path=path,
        )
        return

    if "const" in schema and not _json_equal(instance, schema["const"]):
        raise SchemaValidationError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and not any(
        _json_equal(instance, candidate) for candidate in schema["enum"]
    ):
        raise SchemaValidationError(f"{path}: value is outside the permitted enum")

    declared_type = schema.get("type")
    if declared_type is not None:
        candidates = [declared_type] if isinstance(declared_type, str) else list(declared_type)
        if not any(_matches_type(instance, str(candidate)) for candidate in candidates):
            raise SchemaValidationError(f"{path}: expected type {candidates}, got {type(instance).__name__}")

    if isinstance(instance, Mapping):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                raise SchemaValidationError(f"{path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras:
                raise SchemaValidationError(f"{path}: unexpected properties {extras}")
        for key, value in instance.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, Mapping):
                validate_schema_instance(
                    value,
                    child_schema,
                    root_schema=root,
                    path=f"{path}.{key}",
                )

    if isinstance(instance, list):
        if len(instance) < int(schema.get("minItems", 0)):
            raise SchemaValidationError(f"{path}: too few array items")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in instance]
            if len(encoded) != len(set(encoded)):
                raise SchemaValidationError(f"{path}: array items are not unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, value in enumerate(instance):
                validate_schema_instance(
                    value,
                    item_schema,
                    root_schema=root,
                    path=f"{path}[{index}]",
                )

    if isinstance(instance, str):
        if len(instance) < int(schema.get("minLength", 0)):
            raise SchemaValidationError(f"{path}: string is too short")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(str(pattern), instance) is None:
            raise SchemaValidationError(f"{path}: string does not match {pattern!r}")
        if schema.get("format") == "uuid":
            try:
                parsed = uuid.UUID(instance)
            except ValueError as exc:
                raise SchemaValidationError(f"{path}: invalid UUID") from exc
            if str(parsed) != instance.lower():
                raise SchemaValidationError(f"{path}: UUID is not canonical")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaValidationError(f"{path}: number is below the minimum")
