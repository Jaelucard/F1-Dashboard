"""OAuth2 token acquisition for the OpenF1 live feed.

Confirmed against https://openf1.org/auth.html:
    POST https://api.openf1.org/token
    Content-Type: application/x-www-form-urlencoded
    body: username=<email>&password=<password>
    -> {"access_token": "...", "token_type": "bearer", "expires_in": 3600}

Two things drive the design here:

* **The token is the MQTT password.** It lasts an hour, and an MQTT session
  that outlives its token will be dropped by the broker at an arbitrary moment.
  So the recorder asks for the token *before* each connect and reconnects
  proactively while the current one is still valid, rather than waiting to be
  kicked mid-session. ``refresh_margin_seconds`` is that safety margin.
* **Nothing here may ever log a credential or a token.** Expiry times are
  logged, values are not. ``Token`` therefore has a custom ``__repr__``, because
  a dataclass's default one would print the token in any traceback that carries
  it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

# Ask for a new token once the current one has less than this left.
DEFAULT_REFRESH_MARGIN_SECONDS = 300.0

# Backoff for a failing token endpoint. Capped so a long outage still retries
# roughly every half minute rather than drifting into hours.
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 6

REQUEST_TIMEOUT_SECONDS = 20.0


AuthErrorKind = Literal["credentials", "network", "server", "malformed", "config", "unknown"]


class AuthError(RuntimeError):
    """Token acquisition failed. Message never contains a credential.

    ``kind`` lets the recorder tell "the password is wrong" (retrying will not
    help; the operator must act) from "the endpoint is down" (retrying will).
    """

    def __init__(self, message: str, *, kind: AuthErrorKind = "unknown") -> None:
        super().__init__(message)
        self.kind: AuthErrorKind = kind


@dataclass(repr=False)
class Token:
    """An access token and when it stops being usable."""

    access_token: str = field(repr=False)
    expires_at: datetime

    def seconds_remaining(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.expires_at - now).total_seconds()

    def is_stale(self, margin_seconds: float, now: datetime | None = None) -> bool:
        return self.seconds_remaining(now) <= margin_seconds

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"Token(expires_at={self.expires_at.isoformat()}, value=<redacted>)"

    __str__ = __repr__


class TokenProvider:
    """Fetches, caches and refreshes the OpenF1 access token.

    Synchronous and thread-safe on purpose: the only caller is the recorder's
    supervisor thread, and paho-mqtt is thread-based, so an async API here would
    mean bridging loops for no gain.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        refresh_margin_seconds: float = DEFAULT_REFRESH_MARGIN_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        client: httpx.Client | None = None,
        sleep: "callable[[float], None]" = time.sleep,
    ) -> None:
        self._settings = settings
        self._refresh_margin = refresh_margin_seconds
        self._max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        self._token: Token | None = None
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    @property
    def username(self) -> str:
        """The MQTT/HTTP username. Returned only to be handed to a client."""
        return self._settings.openf1_username.get_secret_value().strip()

    @property
    def current_expiry(self) -> datetime | None:
        """Expiry of the cached token, for /health. Never the token itself."""
        token = self._token
        return token.expires_at if token else None

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token, fetching a new one if needed."""
        with self._lock:
            token = self._token
            if (
                not force_refresh
                and token is not None
                and not token.is_stale(self._refresh_margin)
            ):
                return token.access_token

            token = self._fetch_with_retry()
            self._token = token
            log.info(
                "obtained OpenF1 token, expires at %s (in %.0f min)",
                token.expires_at.isoformat(timespec="seconds"),
                token.seconds_remaining() / 60,
            )
            return token.access_token

    def seconds_until_refresh(self) -> float:
        """How long until the cached token should be replaced. 0 if none/stale."""
        token = self._token
        if token is None:
            return 0.0
        return max(0.0, token.seconds_remaining() - self._refresh_margin)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    # -- internals ----------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        return self._client

    def _fetch_with_retry(self) -> Token:
        self._settings.require_credentials()

        last_error: str = "no attempt made"
        last_kind: AuthErrorKind = "unknown"
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._fetch_once()
            except AuthError as exc:
                last_error = str(exc)
                last_kind = exc.kind
                if attempt == self._max_attempts:
                    break
                delay = min(RETRY_BASE_SECONDS * 2 ** (attempt - 1), RETRY_MAX_SECONDS)
                log.warning(
                    "token request failed (attempt %d/%d): %s - retrying in %.0fs",
                    attempt,
                    self._max_attempts,
                    last_error,
                    delay,
                )
                self._sleep(delay)

        raise AuthError(
            f"could not obtain an OpenF1 token after {self._max_attempts} attempts: "
            f"{last_error}",
            kind=last_kind,
        )

    def _fetch_once(self) -> Token:
        requested_at = datetime.now(timezone.utc)
        try:
            response = self._http().post(
                self._settings.openf1_token_url,
                data={
                    "username": self.username,
                    "password": self._settings.openf1_password.get_secret_value(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            # Stringify the exception type, not the exception: httpx puts the
            # request URL in some messages, and we never want a body echoed.
            raise AuthError(
                f"network error contacting the token endpoint ({type(exc).__name__})",
                kind="network",
            ) from exc

        if response.status_code in (400, 401, 403):
            raise AuthError(
                f"token endpoint rejected the credentials (HTTP {response.status_code}). "
                f"Check OPENF1_USERNAME and OPENF1_PASSWORD in backend/.env.",
                kind="credentials",
            )
        if response.status_code >= 400:
            raise AuthError(f"token endpoint returned HTTP {response.status_code}", kind="server")

        try:
            body = response.json()
        except ValueError as exc:
            raise AuthError("token endpoint returned a non-JSON body", kind="malformed") from exc

        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthError("token endpoint response had no access_token", kind="malformed")

        expires_in = body.get("expires_in", 3600)
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            lifetime = 3600.0
        if lifetime <= 0:
            lifetime = 3600.0

        return Token(
            access_token=access_token,
            expires_at=requested_at + timedelta(seconds=lifetime),
        )
