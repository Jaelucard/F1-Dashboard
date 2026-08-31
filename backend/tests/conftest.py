"""Global test isolation.

Without this, importing the app in a test starts the real recorder against the
real backend/.env: the suite connects to the live OpenF1 broker, uses the
developer's supporter quota, and takes 18 seconds. Tests must never touch the
network or read real credentials, so every test runs with live mode off and
blank credentials, whatever backend/.env happens to contain.

Environment variables outrank the dotenv file in pydantic-settings' precedence
order, so setting them here is enough; the lru_cache on get_settings has to be
cleared either side so no cached instance leaks between tests.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    monkeypatch.setenv("LIVE_MODE", "false")
    monkeypatch.setenv("OPENF1_USERNAME", "")
    monkeypatch.setenv("OPENF1_PASSWORD", "")
    # Never write into the real recordings/ directory from a test.
    monkeypatch.setenv(
        "RECORDINGS_DIR", str(tmp_path_factory.mktemp("recordings"))
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
