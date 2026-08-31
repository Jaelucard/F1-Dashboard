"""The generated frontend files must never drift from the pydantic models.

The build plan requires one SessionState shared by every mode, with the
TypeScript generated from the Python "so they cannot drift". A generator alone
does not guarantee that - someone has to run it. These tests are the guarantee:
change a model without regenerating and the backend suite fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import SessionState
from scripts import generate_fixtures, generate_ts_types


def test_typescript_types_are_up_to_date() -> None:
    if not generate_ts_types.OUTPUT.exists():
        pytest.fail(f"{generate_ts_types.OUTPUT} is missing. Run: make types")
    assert generate_ts_types.OUTPUT.read_text() == generate_ts_types.generate(), (
        "sessionState.ts is stale. Run: make types"
    )


def test_sample_snapshot_fixture_is_up_to_date() -> None:
    if not generate_fixtures.OUTPUT.exists():
        pytest.fail(f"{generate_fixtures.OUTPUT} is missing. Run: make types")
    assert generate_fixtures.OUTPUT.read_text() == generate_fixtures.generate(), (
        "sampleSnapshot.json is stale. Run: make types"
    )


def test_generated_typescript_covers_every_model_field() -> None:
    """A field added to the model must appear in the .ts, not be silently lost."""
    generated = generate_ts_types.generate()
    for field in SessionState.model_fields:
        assert f"  {field}:" in generated, f"{field} missing from the generated types"


def test_fixture_matches_the_session_state_schema() -> None:
    """The fixture the frontend renders must be a valid SessionState."""
    data = json.loads(Path(generate_fixtures.OUTPUT).read_text())
    state = SessionState.model_validate(data)
    assert len(state.drivers) >= 20
    assert state.mode == "live"


def test_generator_rejects_a_schema_it_cannot_render() -> None:
    """Better to fail loudly than to emit `any` and lose type safety."""
    with pytest.raises(generate_ts_types.UnsupportedSchema):
        generate_ts_types.ts_type({"type": "array"})  # no items
    with pytest.raises(generate_ts_types.UnsupportedSchema):
        generate_ts_types.ts_type({"type": "some-future-thing"})


def test_generator_renders_unions_with_null_last() -> None:
    rendered = generate_ts_types.ts_type(
        {"anyOf": [{"type": "null"}, {"type": "number"}, {"type": "string"}]}
    )
    assert rendered == "number | string | null"
