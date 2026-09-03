"""Model additions for replay and the track map."""

from __future__ import annotations

from app.models import DriverState, ReplayInfo, SessionState


def test_replay_info_round_trips() -> None:
    info = ReplayInfo(
        session_key=42, state="paused", speed=2.0, position=None, start=None, end=None,
        progress=0.0, messages_replayed=0, skipped_lines=0, ingest_errors=0,
    )
    assert ReplayInfo.model_validate(info.model_dump()) == info


def test_driver_state_carries_optional_location() -> None:
    driver = DriverState(driver_number=1)
    assert driver.x is None and driver.y is None and driver.location_at is None


def test_session_state_replay_block_defaults_to_none() -> None:
    assert "replay" in SessionState.model_fields
    assert SessionState.model_fields["replay"].default is None
