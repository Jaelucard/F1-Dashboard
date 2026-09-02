"""Feed state and health evaluation, as pure functions.

Three different questions get conflated on a dashboard, and on a race Friday
each one has a different fix:

* Is this backend process alive?          -> ``/health`` always answers 200.
* Can it serve live data right now?       -> ``/ready`` (503 when it cannot).
* Is data actually arriving from OpenF1?  -> ``FeedInfo.state`` on every frame.

Nothing here touches the network or the recorder; it reads the recorder's
health dict, so it can be tested against every state without a broker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import FeedInfo, FeedState


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def compute_feed(
    recorder: dict[str, Any] | None, *, stale_after: float, now: datetime | None = None
) -> FeedInfo:
    """Derive the upstream feed state from the recorder's health dict.

    Data age comes from ``recorder.last_message_at`` - the time the last MQTT
    message was *received* - not from the WebSocket push, which keeps ticking
    whether or not anything arrives.
    """
    now = now or datetime.now(timezone.utc)
    if recorder is None:
        return FeedInfo(state="offline", stale_after_seconds=stale_after)

    connected = bool(recorder.get("connected"))
    auth_state = str(recorder.get("auth_state") or "unauthenticated")
    authenticated = auth_state == "authenticated"
    last_at = _parse(recorder.get("last_message_at"))
    age = max(0.0, (now - last_at).total_seconds()) if last_at is not None else None

    state: FeedState
    if auth_state == "failed":
        state = "auth_failed"
    elif not connected:
        state = "stale" if last_at is not None else "connecting"
    elif age is None:
        state = "connected"
    elif age <= stale_after:
        state = "live"
    else:
        state = "stale"

    return FeedInfo(
        state=state,
        mqtt_connected=connected,
        authenticated=authenticated,
        last_message_at=recorder.get("last_message_at"),
        data_age_seconds=round(age, 3) if age is not None else None,
        stale_after_seconds=stale_after,
        recording_ok=bool(recorder.get("recording_ok", True)),
        last_error=recorder.get("last_error"),
    )


def evaluate_health(
    *,
    live_mode: bool,
    demo_mode: bool,
    credentials_present: bool,
    recorder: dict[str, Any] | None,
    feed: FeedInfo | None,
    now: datetime | None = None,
) -> tuple[str, bool, list[str]]:
    """``(status, ready, problems)``.

    status   "ok" | "degraded" | "error"
    ready    whether the backend can currently do what it was configured for
    problems human-readable, one per issue, for /health and the logs
    """
    problems: list[str] = []
    errors: list[str] = []

    if demo_mode:
        return "ok", True, []
    if not live_mode:
        # Idle by design: replay work, or simply not a race weekend.
        return "ok", True, []

    if not credentials_present:
        errors.append("credentials missing: set OPENF1_USERNAME and OPENF1_PASSWORD in backend/.env")
        return "error", False, errors
    if recorder is None:
        errors.append("recorder is not running (it failed to start; see the logs)")
        return "error", False, errors

    not_ready = False
    auth_state = recorder.get("auth_state")
    if auth_state == "failed":
        errors.append(f"authentication failed: {recorder.get('last_error') or 'see logs'}")
        not_ready = True
    elif not recorder.get("connected"):
        problems.append(f"MQTT not connected ({auth_state or 'unknown'}): {recorder.get('last_error') or 'connecting'}")
        not_ready = True

    if not recorder.get("recording_ok", True):
        errors.append(
            f"recording failing: {recorder.get('recording_error') or recorder.get('last_error') or 'unknown'} "
            f"({recorder.get('write_failures', 0)} failed write(s))"
        )
        not_ready = True

    if feed is not None and feed.state == "stale":
        problems.append(
            f"feed stale: no message for {feed.data_age_seconds:.0f}s"
            if feed.data_age_seconds is not None
            else "feed stale: MQTT dropped after data had arrived"
        )
    if recorder.get("disk_low"):
        problems.append("disk low on the recordings volume")
    dropped = recorder.get("fanout_dropped", 0)
    if dropped:
        problems.append(f"{dropped} message(s) dropped from the live adapter queue (recording unaffected)")
    if recorder.get("may_be_incomplete"):
        problems.append(
            "recording may be incomplete: "
            f"{recorder.get('write_failures', 0)} write failure(s), "
            f"{recorder.get('disconnects_after_data', 0)} disconnect(s) after data, "
            f"~{recorder.get('messages_possibly_lost', 0)} message(s) possibly lost"
        )

    if errors:
        return "error", False, errors + problems
    if problems:
        return "degraded", not not_ready, problems
    return "ok", not not_ready, []
