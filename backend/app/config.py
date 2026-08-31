"""Configuration loaded from backend/.env.

Design notes
------------
* Credentials are held as ``SecretStr`` so that an accidental ``print(settings)``,
  a pydantic validation error, or a FastAPI exception page renders them as
  ``**********`` instead of the real value. The plaintext is only reachable via
  an explicit ``.get_secret_value()`` call, which appears exactly once, in
  ``auth.py``.
* Missing credentials are *not* a validation error at import time. The app must
  still boot without them (Phase 0, and any offline replay work in Tier B), so
  the check is a separate explicit call made only by the code paths that need
  live access.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


class MissingCredentials(RuntimeError):
    """Raised when live mode is requested but a credential variable is empty."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openf1_username: SecretStr = SecretStr("")
    openf1_password: SecretStr = SecretStr("")
    live_mode: bool = False

    demo_mode: bool = False
    """Load a synthetic 22-car grid instead of connecting to OpenF1.

    Exists so the UI can be checked away from a session window - there is no
    live data on a Tuesday, and waiting until Friday to discover the leaderboard
    is broken would be a poor plan. Never connects to anything, and is off
    unless explicitly enabled.
    """

    # Where recorder.py writes recordings/<session_key>/<topic>.jsonl
    recordings_dir: Path = REPO_ROOT / "recordings"

    # OpenF1 endpoints, confirmed against https://openf1.org/auth.html
    openf1_token_url: str = "https://api.openf1.org/token"
    openf1_api_base: str = "https://api.openf1.org/v1"
    mqtt_host: str = "mqtt.openf1.org"
    mqtt_port: int = 8883

    def require_credentials(self) -> None:
        """Fail loudly, naming the variable but never its value."""
        missing = [
            name
            for name, value in (
                ("OPENF1_USERNAME", self.openf1_username),
                ("OPENF1_PASSWORD", self.openf1_password),
            )
            if not value.get_secret_value().strip()
        ]
        if missing:
            raise MissingCredentials(
                f"{', '.join(missing)} is empty or missing. "
                f"Copy backend/.env.example to backend/.env and fill it in."
            )

    @property
    def credentials_present(self) -> bool:
        try:
            self.require_credentials()
        except MissingCredentials:
            return False
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
