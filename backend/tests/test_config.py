"""Credentials must never be reachable by accident, and must fail loudly."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from pathlib import Path

from app.config import REPO_ROOT, MissingCredentials, Settings, describe_recordings_dir

SENTINEL_USER = "test-user@example.invalid"
SENTINEL_PASS = "test-password-placeholder"


def _settings(**overrides: object) -> Settings:
    # _env_file=None stops pydantic-settings reading the developer's real
    # backend/.env, so the test suite can never touch live credentials.
    base: dict[str, object] = {
        "openf1_username": SecretStr(SENTINEL_USER),
        "openf1_password": SecretStr(SENTINEL_PASS),
        "live_mode": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_secrets_are_not_in_repr_or_str() -> None:
    settings = _settings()
    for rendered in (repr(settings), str(settings), repr(settings.model_dump())):
        assert SENTINEL_PASS not in rendered
        assert SENTINEL_USER not in rendered


def test_secret_value_requires_explicit_unwrap() -> None:
    settings = _settings()
    assert settings.openf1_password.get_secret_value() == SENTINEL_PASS


@pytest.mark.parametrize(
    ("overrides", "expected_name", "other_name"),
    [
        ({"openf1_username": SecretStr("")}, "OPENF1_USERNAME", "OPENF1_PASSWORD"),
        ({"openf1_password": SecretStr("   ")}, "OPENF1_PASSWORD", "OPENF1_USERNAME"),
    ],
)
def test_missing_credential_names_the_variable_not_the_value(
    overrides: dict[str, object], expected_name: str, other_name: str
) -> None:
    settings = _settings(**overrides)
    with pytest.raises(MissingCredentials) as excinfo:
        settings.require_credentials()

    message = str(excinfo.value)
    assert expected_name in message
    assert other_name not in message
    # The error message is the most likely place for a leak, so assert directly.
    assert SENTINEL_PASS not in message
    assert SENTINEL_USER not in message


def test_credentials_present_flag() -> None:
    assert _settings().credentials_present is True
    assert _settings(openf1_password=SecretStr("")).credentials_present is False


def test_describe_recordings_dir_never_returns_an_absolute_path() -> None:
    assert describe_recordings_dir(REPO_ROOT / "recordings") == "recordings"
    assert describe_recordings_dir(Path("captures/today")) == "captures/today"
    elsewhere = describe_recordings_dir(Path("/somewhere/private/recordings"))
    assert not elsewhere.startswith("/")
    assert "somewhere" not in elsewhere
