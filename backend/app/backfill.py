"""REST backfill so a mid-session connect knows what session it is watching.

The live adapter learns the session and the entry list from the ``v1/sessions``
and ``v1/drivers`` MQTT announcements, which OpenF1 publishes *once*, around the
time a session starts. Connect after that and the timing topics flow normally
but those two never arrive: ``_build_session`` returns None (the strip reads NO
SESSION), ``circuit_key`` is null so no outline loads, and every driver row is a
bare number because there is no ``name_acronym`` or ``team_colour``.

So when the active session's bucket holds no ``v1/sessions`` or no
``v1/drivers`` records, fetch the same data over REST and feed it through
``OpenF1LiveSource.ingest`` with the matching topic name. Nothing here parses a
payload: validation, natural keys and session scoping all apply unchanged,
which is why a backfilled row and an MQTT row are indistinguishable downstream.

What is deliberately *not* fetched:

* ``/car_data`` - it must be windowed (a whole-session request answers
  "No results found"), and the live stream covers it from the moment we connect.
* ``/location`` - the same, and it is by far the largest topic.

Backfilled rows are counted separately (``AdapterInfo.backfilled_records``) so
``messages_seen`` stays a count of what actually came off the wire.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx

from app.adapters.openf1_live import (
    TOPIC_DRIVERS,
    TOPIC_LAPS,
    TOPIC_PIT,
    TOPIC_RACE_CONTROL,
    TOPIC_SESSIONS,
    TOPIC_STINTS,
    OpenF1LiveSource,
)

log = logging.getLogger(__name__)

# (REST path, MQTT topic name). Order matters: identity first, so the strip and
# the map come right even if a later request fails.
BACKFILL_TOPICS: tuple[tuple[str, str], ...] = (
    ("sessions", TOPIC_SESSIONS),
    ("drivers", TOPIC_DRIVERS),
    ("laps", TOPIC_LAPS),
    ("stints", TOPIC_STINTS),
    ("pit", TOPIC_PIT),
    ("race_control", TOPIC_RACE_CONTROL),
)

# The two topics whose absence is the whole reason this module exists.
IDENTITY_TOPICS: tuple[str, ...] = (TOPIC_SESSIONS, TOPIC_DRIVERS)

RETRY_AFTER_SECONDS = 60.0
"""How long to wait before trying again after a failed or incomplete run."""

POLL_INTERVAL_SECONDS = 2.0
"""How often the service looks at the adapter. Cheap: an in-memory length."""

REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass
class BackfillResult:
    """What one pass managed to fetch. Reported, never raised."""

    session_key: int
    rows: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.rows.values())

    @property
    def ok(self) -> bool:
        return not self.errors


def missing_identity_topics(source: OpenF1LiveSource) -> list[str]:
    """Which of ``v1/sessions`` / ``v1/drivers`` the active session has nothing for."""
    return [topic for topic in IDENTITY_TOPICS if source.record_count(topic) == 0]


def needs_backfill(source: OpenF1LiveSource) -> bool:
    """True when the active session is missing its identity, so the board would
    show NO SESSION or bare driver numbers."""
    if source.active_session_key is None:
        return False
    return bool(missing_identity_topics(source))


class SessionBackfill:
    """One pass of REST fetches into an adapter. No loop, no state beyond auth."""

    def __init__(
        self,
        source: OpenF1LiveSource,
        client: httpx.AsyncClient,
        *,
        api_base: str,
        token_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._source = source
        self._client = client
        self._api_base = api_base.rstrip("/")
        self._token_getter = token_getter

    async def _token(self) -> str | None:
        """The bearer token, fetched off the event loop.

        ``TokenProvider.get_token`` is synchronous and may do HTTP on a refresh,
        so it never runs inline on the loop.
        """
        if self._token_getter is None:
            return None
        try:
            return await asyncio.to_thread(self._token_getter)
        except Exception as exc:  # noqa: BLE001 - auth failure must not stop the pass
            log.warning("backfill could not obtain a token: %s", type(exc).__name__)
            return None

    async def _fetch(self, path: str, session_key: int, token: str | None) -> list[dict[str, Any]]:
        """One REST page. Retries once without the token on a 401/403.

        OpenF1 requires auth for real-time REST on the supporter tier, but the
        same data is public once a session is historical - so an unauthenticated
        retry is worth exactly one attempt before giving up.
        """
        url = f"{self._api_base}/{path}"
        params = {"session_key": session_key}
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        response = await self._client.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code in (401, 403) and headers:
            log.warning(
                "backfill %s: HTTP %d with a token, retrying unauthenticated",
                path,
                response.status_code,
            )
            response = await self._client.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()

        body = response.json()
        if not isinstance(body, list):
            # OpenF1 answers a dict ({"detail": ...}) for a restricted or
            # oversized request. Not a row source; say so rather than crash.
            detail = body.get("detail") if isinstance(body, dict) else None
            raise ValueError(str(detail or "response was not a list of rows"))
        return [row for row in body if isinstance(row, dict)]

    def _ingest(self, topic: str, rows: Iterable[dict[str, Any]]) -> int:
        applied = 0
        for row in rows:
            if self._source.ingest(topic, row, backfill=True):
                applied += 1
        return applied

    async def run(self, session_key: int, *, topics: Iterable[tuple[str, str]] = BACKFILL_TOPICS) -> BackfillResult:
        """Fetch and ingest every topic for one session. Never raises."""
        result = BackfillResult(session_key=session_key)
        token = await self._token()

        for path, topic in topics:
            try:
                rows = await self._fetch(path, session_key, token)
            except httpx.HTTPStatusError as exc:
                result.errors[path] = f"HTTP {exc.response.status_code}"
                log.warning("backfill %s for session %s: HTTP %d", path, session_key, exc.response.status_code)
                continue
            except (httpx.HTTPError, ValueError) as exc:
                result.errors[path] = type(exc).__name__
                log.warning("backfill %s for session %s failed: %s", path, session_key, exc)
                continue
            result.rows[topic] = self._ingest(topic, rows)

        log.info(
            "backfilled session %s: %s%s",
            session_key,
            ", ".join(f"{t.split('/')[-1]}={n}" for t, n in sorted(result.rows.items())) or "nothing",
            f" (failed: {', '.join(sorted(result.errors))})" if result.errors else "",
        )
        return result


class BackfillService:
    """Watches an adapter and backfills whenever the active session lacks identity.

    Runs as an asyncio task, never on the MQTT thread. ``nudge`` is safe to call
    from any thread and is what the adapter's ``on_session_switch`` hook uses, so
    a switch is picked up at once rather than at the next poll.
    """

    def __init__(
        self,
        source: OpenF1LiveSource,
        *,
        api_base: str,
        token_getter: Callable[[], str | None] | None = None,
        client: httpx.AsyncClient | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        retry_after: float = RETRY_AFTER_SECONDS,
    ) -> None:
        self._source = source
        self._api_base = api_base
        self._token_getter = token_getter
        self._client = client
        self._owns_client = client is None
        self._poll = poll_interval
        self._retry_after = retry_after

        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._done: dict[int, float] = {}
        self.runs = 0
        self.last_result: BackfillResult | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._run(), name="backfill")

    def cancel(self) -> None:
        """Stop without awaiting. For the synchronous teardown paths.

        The client is left to be garbage collected: it owns no thread and no
        file handle, and a replay unload must not block on network teardown.
        """
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def nudge(self, previous: int | None = None, session_key: int | None = None) -> None:
        """Ask for a pass now. Thread-safe: the adapter calls this from MQTT."""
        if session_key is not None:
            self._done.pop(session_key, None)
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            pass  # loop already closed; shutting down

    # -- the loop -----------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a backfill bug must never stop the backend
                log.exception("backfill pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            self._wake.clear()

    async def _tick(self) -> None:
        session_key = self._source.active_session_key
        if session_key is None or not needs_backfill(self._source):
            return
        now = asyncio.get_running_loop().time()
        attempted_at = self._done.get(session_key)
        if attempted_at is not None and now - attempted_at < self._retry_after:
            return
        self._done[session_key] = now
        self.runs += 1
        self.last_result = await self.run_once(session_key)

    async def run_once(self, session_key: int) -> BackfillResult:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
            self._owns_client = True
        backfill = SessionBackfill(
            self._source, self._client, api_base=self._api_base, token_getter=self._token_getter
        )
        return await backfill.run(session_key)
