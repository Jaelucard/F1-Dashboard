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


def describe_recordings_dir(path: Path) -> str:
    """A loggable description of ``recordings_dir`` that never reveals an
    absolute filesystem path.

    Returns the repository-relative path when the directory lives inside the
    checkout (the default), the path as given when it is already relative,
    and a generic placeholder otherwise. Never resolves symlinks.
    """
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return "<outside the repository, set by RECORDINGS_DIR>"


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

    recording_enabled: bool = True
    """Whether the recorder writes to disk at all. Off is for a machine that is
    short on space and only needs the live dashboard; the FP1 recording that
    resolves what aero_raw means in 2026, and every replay, both depend on a
    recording existing, so leave this on unless you have a specific reason not
    to. When false the recorder still connects and feeds the live adapter -
    the dashboard keeps working - it just never touches disk."""

    recorder_min_free_bytes: int = 500 * 1024 * 1024
    """Below this much free space on the recordings volume, /health reports
    ``disk_low`` and readiness degrades. Recording continues regardless: the
    capture is the artefact, and stopping early would lose more than it saves."""

    mqtt_qos: int = 1
    """Subscription QoS. 1 asks the broker to retransmit anything unacknowledged
    across a brief drop; the adapter is idempotent so a redelivery is harmless.
    The broker may downgrade the grant."""

    fanout_queue_size: int = 10_000
    """Bound on messages waiting between the MQTT thread and the live adapter.
    Overflow drops from the *queue only* - never from the recording - and is
    counted in /health as ``fanout_dropped``."""

    feed_stale_seconds: float = 15.0
    """No message for this long while connected = feed shown as STALE, not LIVE.
    Location and car data arrive several times a second during a session."""

    # -- WebSocket policy -------------------------------------------------
    ws_allowed_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:8000,http://127.0.0.1:8000"
    )
    """Comma-separated browser origins allowed to open /ws. A browser always
    sends Origin; a request from any other page is refused before accept."""

    ws_require_origin: bool = False
    """Also refuse connections with no Origin header (curl, websocat). Off by
    default so command-line checks keep working on a local machine."""

    ws_auth_token: SecretStr = SecretStr("")
    """Optional shared secret. When set, /ws requires ``?token=<value>`` or an
    ``Authorization: Bearer <value>`` header. The browser reads it from
    ``VITE_WS_TOKEN`` at build time or ``localStorage['f1dash.wsToken']``."""

    ws_max_clients: int = 16
    ws_send_timeout_seconds: float = 5.0
    """A browser that cannot take a frame within this is dropped, so a stalled
    tab cannot pile up frames or slow the others down."""
    ws_push_interval_seconds: float = 1.0

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
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(o.strip().rstrip("/").lower() for o in self.ws_allowed_origins.split(",") if o.strip())

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
