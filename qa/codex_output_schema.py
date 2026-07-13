#!/usr/bin/env python3
"""Build and verify the strict Codex authoring schema.

The release gate intentionally uses a richer JSON Schema than OpenAI
Structured Outputs accepts.  This module projects that gate schema into the
strict subset used by ``codex exec --output-schema`` while preserving the
same JSON value shape.  The deterministic release gate remains authoritative.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


QA_ROOT = Path(__file__).resolve().parent
DEFAULT_GATE_SCHEMA = QA_ROOT / "schemas" / "run-result.schema.json"
DEFAULT_AUTHORING_SCHEMA = QA_ROOT / "schemas" / "codex-run-result.schema.json"
DEFAULT_COVERAGE = QA_ROOT / "coverage-lock.json"

_ALLOWED_SCHEMA_KEYS = {
    "$defs",
    "$ref",
    "additionalProperties",
    "anyOf",
    "enum",
    "items",
    "properties",
    "required",
    "type",
}
_ALLOWED_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_IGNORED_GATE_CONDITIONALS = {
    ("$defs", "attemptResult"),
    ("$defs", "scenarioResult"),
}
_UNSUPPORTED_COMPOSITION = {
    "allOf",
    "dependentRequired",
    "dependentSchemas",
    "else",
    "if",
    "not",
    "oneOf",
    "then",
}


class AuthoringSchemaError(ValueError):
    """Raised when the gate schema cannot be projected safely."""


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuthoringSchemaError(f"{path} must contain a JSON object")
    return value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    raise AuthoringSchemaError(f"unsupported const value type: {type(value).__name__}")


def _assertion_schema(coverage: dict[str, Any]) -> dict[str, Any]:
    scenario_ids = coverage.get("required_scenarios")
    contracts = coverage.get("scenario_contracts")
    if not isinstance(scenario_ids, list) or not isinstance(contracts, dict):
        raise AuthoringSchemaError("coverage is missing scenario assertion contracts")

    variants: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        contract = contracts.get(scenario_id)
        if not isinstance(contract, dict):
            raise AuthoringSchemaError(f"coverage contract missing for {scenario_id!r}")
        names = contract.get("required_assertions")
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or not name for name in names)
            or len(names) != len(set(names))
        ):
            raise AuthoringSchemaError(
                f"coverage assertions for {scenario_id!r} must be unique strings"
            )
        variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": list(names),
                "properties": {name: {"type": "boolean"} for name in names},
            }
        )
    return {"anyOf": variants}


def _project_node(
    node: Any,
    *,
    path: tuple[str, ...],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise AuthoringSchemaError(
            f"schema node at {'.'.join(path) or '$'} is not an object"
        )

    if path == ("$defs", "scenarioResult", "properties", "assertions"):
        return _assertion_schema(coverage)

    if "allOf" in node and path not in _IGNORED_GATE_CONDITIONALS:
        raise AuthoringSchemaError(
            f"new gate-only allOf at {'.'.join(path) or '$'} needs an explicit projection"
        )
    for keyword in _UNSUPPORTED_COMPOSITION - {"allOf", "oneOf"}:
        if keyword in node:
            raise AuthoringSchemaError(
                f"unsupported composition keyword {keyword!r} at {'.'.join(path) or '$'}"
            )

    if "oneOf" in node:
        if set(node) != {"oneOf"}:
            raise AuthoringSchemaError(
                f"oneOf at {'.'.join(path) or '$'} must not have sibling constraints"
            )
        alternatives = node["oneOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise AuthoringSchemaError(
                f"oneOf at {'.'.join(path) or '$'} must be non-empty"
            )
        return {
            "anyOf": [
                _project_node(
                    item, path=path + ("anyOf", str(index)), coverage=coverage
                )
                for index, item in enumerate(alternatives)
            ]
        }

    if "const" in node:
        value = deepcopy(node["const"])
        return {"type": _json_type(value), "enum": [value]}

    if "$ref" in node:
        if set(node) != {"$ref"} or not isinstance(node["$ref"], str):
            raise AuthoringSchemaError(
                f"$ref at {'.'.join(path) or '$'} must be the only keyword"
            )
        return {"$ref": node["$ref"]}

    output: dict[str, Any] = {}
    schema_type = node.get("type")
    if schema_type is not None:
        output["type"] = deepcopy(schema_type)
    if "enum" in node:
        output["enum"] = deepcopy(node["enum"])
    if "anyOf" in node:
        alternatives = node["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise AuthoringSchemaError(
                f"anyOf at {'.'.join(path) or '$'} must be non-empty"
            )
        output["anyOf"] = [
            _project_node(item, path=path + ("anyOf", str(index)), coverage=coverage)
            for index, item in enumerate(alternatives)
        ]

    if schema_type == "object":
        properties = node.get("properties")
        if not isinstance(properties, dict):
            raise AuthoringSchemaError(
                f"object at {'.'.join(path) or '$'} must declare properties"
            )
        declared_required = node.get("required")
        if set(declared_required or []) != set(properties):
            raise AuthoringSchemaError(
                f"gate object at {'.'.join(path) or '$'} must require every property"
            )
        if node.get("additionalProperties") is not False:
            raise AuthoringSchemaError(
                f"dynamic object at {'.'.join(path) or '$'} needs an explicit projection"
            )
        output["additionalProperties"] = False
        output["required"] = list(declared_required)
        output["properties"] = {
            name: _project_node(
                child,
                path=path + ("properties", name),
                coverage=coverage,
            )
            for name, child in properties.items()
        }
    elif schema_type == "array":
        if "items" not in node:
            raise AuthoringSchemaError(
                f"array at {'.'.join(path) or '$'} must declare items"
            )
        output["items"] = _project_node(
            node["items"], path=path + ("items",), coverage=coverage
        )

    if "$defs" in node:
        definitions = node["$defs"]
        if not isinstance(definitions, dict):
            raise AuthoringSchemaError("$defs must be an object")
        output["$defs"] = {
            name: _project_node(child, path=("$defs", name), coverage=coverage)
            for name, child in definitions.items()
        }

    if not output or not ({"type", "$ref", "anyOf"} & set(output)):
        raise AuthoringSchemaError(
            f"schema node at {'.'.join(path) or '$'} has no compatible type or reference"
        )
    return output


def build_authoring_schema(
    gate_schema: dict[str, Any], coverage: dict[str, Any]
) -> dict[str, Any]:
    """Return the strict Structured Outputs projection of the gate schema."""

    projected = _project_node(gate_schema, path=(), coverage=coverage)
    errors = validate_authoring_schema(projected)
    if errors:
        raise AuthoringSchemaError("; ".join(errors))
    return projected


def validate_authoring_schema(schema: dict[str, Any]) -> list[str]:
    """Check the OpenAI Structured Outputs subset without making a network call."""

    errors: list[str] = []
    property_count = 0
    enum_count = 0
    refs: list[tuple[str, str]] = []

    def walk(node: Any, path: str, object_depth: int) -> None:
        nonlocal property_count, enum_count
        if not isinstance(node, dict):
            errors.append(f"{path}: schema node must be an object")
            return
        unknown = set(node) - _ALLOWED_SCHEMA_KEYS
        if unknown:
            errors.append(f"{path}: unsupported keywords {sorted(unknown)}")
        if _UNSUPPORTED_COMPOSITION & set(node):
            errors.append(
                f"{path}: unsupported composition {sorted(_UNSUPPORTED_COMPOSITION & set(node))}"
            )

        if "$ref" in node:
            if set(node) != {"$ref"} or not isinstance(node["$ref"], str):
                errors.append(f"{path}: $ref must be a string and the only keyword")
            else:
                refs.append((path, node["$ref"]))
            return

        schema_type = node.get("type")
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if schema_type is not None:
            if not types or any(item not in _ALLOWED_TYPES for item in types):
                errors.append(f"{path}: invalid type {schema_type!r}")
            if len(types) != len(set(types)):
                errors.append(f"{path}: duplicate type alternatives")
        elif "anyOf" not in node:
            errors.append(f"{path}: node must have type, $ref, or anyOf")

        if path == "$" and schema_type != "object":
            errors.append("$: root must be an object")
        if path == "$" and "anyOf" in node:
            errors.append("$: root must not use anyOf")

        if "object" in types:
            properties = node.get("properties")
            required = node.get("required")
            if not isinstance(properties, dict):
                errors.append(f"{path}: object must declare properties")
                properties = {}
            if node.get("additionalProperties") is not False:
                errors.append(f"{path}: object must set additionalProperties to false")
            if not isinstance(required, list) or required != list(properties):
                errors.append(
                    f"{path}: required must list every property in schema order"
                )
            property_count += len(properties)
            if object_depth + 1 > 10:
                errors.append(f"{path}: object nesting exceeds 10 levels")
            for name, child in properties.items():
                walk(child, f"{path}.properties.{name}", object_depth + 1)
        elif (
            "properties" in node or "required" in node or "additionalProperties" in node
        ):
            errors.append(f"{path}: object keywords require object type")

        if "array" in types:
            if "items" not in node:
                errors.append(f"{path}: array must declare items")
            else:
                walk(node["items"], f"{path}.items", object_depth)
        elif "items" in node:
            errors.append(f"{path}: items requires array type")

        if "enum" in node:
            values = node["enum"]
            if not isinstance(values, list) or not values:
                errors.append(f"{path}: enum must be a non-empty array")
            else:
                enum_count += len(values)
                if len(values) > 250 and all(
                    isinstance(value, str) for value in values
                ):
                    if sum(len(value) for value in values) > 15_000:
                        errors.append(f"{path}: enum string length exceeds 15,000")

        if "anyOf" in node:
            alternatives = node["anyOf"]
            if not isinstance(alternatives, list) or not alternatives:
                errors.append(f"{path}: anyOf must be a non-empty array")
            else:
                for index, child in enumerate(alternatives):
                    walk(child, f"{path}.anyOf[{index}]", object_depth)

        definitions = node.get("$defs")
        if definitions is not None:
            if path != "$" or not isinstance(definitions, dict):
                errors.append(f"{path}: $defs is only allowed as an object at the root")
            else:
                for name, child in definitions.items():
                    walk(child, f"$.$defs.{name}", 0)

    walk(schema, "$", 0)
    definitions = schema.get("$defs", {}) if isinstance(schema, dict) else {}
    for path, reference in refs:
        prefix = "#/$defs/"
        if (
            not reference.startswith(prefix)
            or reference[len(prefix) :] not in definitions
        ):
            errors.append(f"{path}: unresolved local $ref {reference!r}")
    if property_count > 5_000:
        errors.append(f"$: schema has {property_count} properties; maximum is 5000")
    if enum_count > 1_000:
        errors.append(f"$: schema has {enum_count} enum values; maximum is 1000")

    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # pragma: no cover - detailed subset checks normally win
        errors.append(f"$: invalid JSON Schema: {exc}")
    return errors


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate-schema", type=Path, default=DEFAULT_GATE_SCHEMA)
    parser.add_argument(
        "--authoring-schema", type=Path, default=DEFAULT_AUTHORING_SCHEMA
    )
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--print", action="store_true", dest="print_schema")
    args = parser.parse_args()

    expected = build_authoring_schema(
        _load_json(args.gate_schema), _load_json(args.coverage)
    )
    if args.print_schema:
        print(_canonical_json(expected), end="")
        return 0

    actual = _load_json(args.authoring_schema)
    errors = validate_authoring_schema(actual)
    if actual != expected:
        errors.append(
            "authoring schema is stale; regenerate it from the gate schema and coverage lock"
        )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Codex authoring schema: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
