"""Generate the frontend's SessionState types from the pydantic models.

The build plan requires one SessionState shared by every mode, with the
TypeScript generated from the Python "so they cannot drift". This script is
that generator, and ``tests/test_generated_types.py`` fails if the checked-in
.ts file does not match what this produces - so drift breaks the build rather
than surfacing as a runtime surprise mid-session.

Why hand-rolled rather than json-schema-to-typescript: that would add an npm
dependency and a second toolchain to a project whose network is slow, to
translate a schema we control completely. The subset of JSON Schema pydantic
emits for these models is small and fully handled below; anything outside it
raises rather than silently emitting `any`.

Every field is emitted as required, because ``model_dump()`` always includes
every field. Optionality is expressed as ``| null``, which is what actually
arrives over the wire.

Usage:  .venv/bin/python -m scripts.generate_ts_types [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import SessionState  # noqa: E402

OUTPUT = (
    Path(__file__).resolve().parent.parent.parent
    / "frontend"
    / "src"
    / "types"
    / "sessionState.ts"
)

HEADER = """/**
 * GENERATED FILE - DO NOT EDIT BY HAND.
 *
 * Generated from backend/app/models.py by backend/scripts/generate_ts_types.py.
 * Regenerate with:  make types
 *
 * A backend test fails if this file drifts from the pydantic models, so the two
 * sides of the WebSocket cannot disagree about the shape of a snapshot.
 */
"""


class UnsupportedSchema(RuntimeError):
    """The models grew a construct the generator does not handle."""


def ts_type(schema: dict[str, Any]) -> str:
    """Render one JSON Schema node as a TypeScript type expression."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]

    if "const" in schema:
        return _literal(schema["const"])

    if "enum" in schema:
        return " | ".join(_literal(value) for value in schema["enum"])

    if "anyOf" in schema:
        parts = [ts_type(option) for option in schema["anyOf"]]
        # Keep `null` last so unions read as `string | null`, not `null | string`.
        non_null = [p for p in parts if p != "null"]
        ordered = non_null + (["null"] if "null" in parts else [])
        return " | ".join(dict.fromkeys(ordered))

    kind = schema.get("type")
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        items = schema.get("items")
        if not items:
            raise UnsupportedSchema("array without items")
        inner = ts_type(items)
        return f"({inner})[]" if " " in inner else f"{inner}[]"
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {ts_type(extra)}>"
        return "Record<string, unknown>"

    raise UnsupportedSchema(f"cannot render schema node: {schema!r}")


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _docstring(schema: dict[str, Any], indent: str) -> list[str]:
    text = schema.get("description")
    if not text:
        return []
    # Python docstrings use RST double backticks; JSDoc wants markdown singles.
    text = text.replace("``", "`")
    lines = [line.rstrip() for line in text.strip().split("\n")]
    if len(lines) == 1:
        return [f"{indent}/** {lines[0]} */"]
    out = [f"{indent}/**"]
    out.extend(f"{indent} * {line}".rstrip() for line in lines)
    out.append(f"{indent} */")
    return out


def render_interface(name: str, schema: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.extend(_docstring(schema, ""))
    lines.append(f"export interface {name} {{")
    for field, field_schema in schema.get("properties", {}).items():
        lines.extend(_docstring(field_schema, "  "))
        lines.append(f"  {field}: {ts_type(field_schema)}")
    lines.append("}")
    return "\n".join(lines)


def generate() -> str:
    schema = SessionState.model_json_schema(ref_template="#/$defs/{model}")
    blocks: list[str] = [HEADER]

    for name, definition in sorted(schema.get("$defs", {}).items()):
        blocks.append(render_interface(name, definition))

    root = {k: v for k, v in schema.items() if k != "$defs"}
    blocks.append(render_interface(schema.get("title", "SessionState"), root))

    return "\n\n".join(blocks) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the checked-in file is stale, without writing",
    )
    args = parser.parse_args()

    generated = generate()
    if args.check:
        if not OUTPUT.exists():
            print(f"{OUTPUT} does not exist. Run: make types", file=sys.stderr)
            return 1
        if OUTPUT.read_text() != generated:
            print(f"{OUTPUT} is stale. Run: make types", file=sys.stderr)
            return 1
        print(f"{OUTPUT.name} is up to date")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
