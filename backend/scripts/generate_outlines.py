"""Generate bundled circuit outlines from OpenF1's own historical location data.

For every circuit raced in a year, take the latest completed session (a race
when there is one), find the fastest clean lap, fetch that lap's ``location``
samples for that driver, simplify them to at most ``MAX_POINTS`` points, and
write ``frontend/src/data/circuits/<circuit_key>.json``.

Why from OpenF1 rather than a drawing: the outline is then in the same
coordinate frame as the live ``location`` stream, so cars land on the line
with no per-circuit calibration, and attribution stays with OpenF1.

Historical data needs no credentials. This script never imports ``app.config``
and so cannot read ``backend/.env``.

Usage:  .venv/bin/python -m scripts.generate_outlines [--year 2026] [--circuit KEY] [--out DIR]
        make outlines
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.openf1.org"
DEFAULT_OUT = Path(__file__).resolve().parent.parent.parent / "frontend" / "src" / "data" / "circuits"
MAX_POINTS = 400
MIN_POINTS = 10
REQUEST_INTERVAL = 0.4
"""Seconds between requests: the free tier allows 3 per second."""

Point = tuple[float, float]
Sleep = Callable[[float], None]


# -- pure pieces ----------------------------------------------------------------


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def candidate_sessions(sessions: list[dict[str, Any]], now: datetime) -> dict[int, list[dict[str, Any]]]:
    """Per circuit_key, the completed sessions to try, best first: races before
    other types, then most recent. OpenF1 answers 404 for a session it has no
    data for, so the generator walks this list until one works."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for session in sessions:
        key = session.get("circuit_key")
        end = _parse(session.get("date_end"))
        if not isinstance(key, int) or end is None or end >= now:
            continue
        grouped.setdefault(key, []).append(session)
    return {key: sorted(group, key=_rank, reverse=True) for key, group in grouped.items()}


def latest_completed_sessions(sessions: list[dict[str, Any]], now: datetime) -> dict[int, dict[str, Any]]:
    """Per circuit_key, the preferred session: the first candidate."""
    return {key: group[0] for key, group in candidate_sessions(sessions, now).items()}


def _rank(session: dict[str, Any]) -> tuple[bool, datetime]:
    return (session.get("session_type") == "Race", _parse(session.get("date_end")) or datetime.min.replace(tzinfo=timezone.utc))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fastest_clean_lap(laps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The quickest lap that has a start time, all three sectors and no pit exit."""
    clean = [
        lap for lap in laps
        if _is_number(lap.get("lap_duration"))
        and not lap.get("is_pit_out_lap")
        and all(_is_number(lap.get(f"duration_sector_{n}")) for n in (1, 2, 3))
        and _parse(lap.get("date_start")) is not None
        and isinstance(lap.get("driver_number"), int)
    ]
    return min(clean, key=lambda lap: lap["lap_duration"]) if clean else None


def _perpendicular_distance(point: Point, start: Point, end: Point) -> float:
    (x, y), (x1, y1), (x2, y2) = point, start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / math.hypot(dx, dy)


def _douglas_peucker(points: list[Point], epsilon: float) -> list[Point]:
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        best, index = 0.0, -1
        for i in range(first + 1, last):
            distance = _perpendicular_distance(points[i], points[first], points[last])
            if distance > best:
                best, index = distance, i
        if index != -1 and best > epsilon:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def simplify(points: list[Point], max_points: int = MAX_POINTS) -> list[Point]:
    """Douglas-Peucker with the tolerance doubled until under ``max_points``.
    Always keeps the first and last point."""
    if len(points) <= max_points:
        return list(points)
    epsilon = 1.0
    result = _douglas_peucker(points, epsilon)
    while len(result) > max_points:
        epsilon *= 2
        result = _douglas_peucker(points, epsilon)
    return result


# -- fetching -------------------------------------------------------------------


def _get(client: httpx.Client, path: str, params: dict[str, Any], sleep: Sleep) -> list[dict[str, Any]]:
    sleep(REQUEST_INTERVAL)
    response = client.get(path, params=params)
    response.raise_for_status()
    data = response.json()
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def build_outline(
    client: httpx.Client, session: dict[str, Any], *, sleep: Sleep, now: datetime
) -> dict[str, Any] | None:
    session_key = session["session_key"]
    lap = fastest_clean_lap(_get(client, "/v1/laps", {"session_key": session_key}, sleep))
    if lap is None:
        return None
    start = _parse(lap["date_start"])
    assert start is not None
    end = start + timedelta(seconds=float(lap["lap_duration"]))
    rows = _get(
        client,
        "/v1/location",
        {"session_key": session_key, "driver_number": lap["driver_number"],
         "date>": start.isoformat(), "date<": end.isoformat()},
        sleep,
    )
    points: list[Point] = [
        (float(row["x"]), float(row["y"])) for row in rows if _is_number(row.get("x")) and _is_number(row.get("y"))
    ]
    if len(points) < MIN_POINTS:
        return None
    return {
        "circuit_key": session["circuit_key"],
        "circuit_short_name": session.get("circuit_short_name"),
        "source_session_key": session_key,
        "generated_at": now.isoformat(timespec="seconds"),
        "points": [[x, y] for x, y in simplify(points)],
    }


def generate(
    client: httpx.Client,
    year: int,
    out_dir: Path,
    *,
    circuit: int | None = None,
    sleep: Sleep = time.sleep,
    now: datetime | None = None,
    report: Callable[[str], None] = print,
) -> list[Path]:
    now = now or datetime.now(timezone.utc)
    candidates = candidate_sessions(_get(client, "/v1/sessions", {"year": year}, sleep), now)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key in sorted(candidates):
        if circuit is not None and key != circuit:
            continue
        outline: dict[str, Any] | None = None
        label = f"{key} {candidates[key][0].get('circuit_short_name') or ''}".strip()
        reasons: list[str] = []
        for session in candidates[key]:
            try:
                outline = build_outline(client, session, sleep=sleep, now=now)
            except httpx.HTTPStatusError as exc:
                reasons.append(f"session {session.get('session_key')}: HTTP {exc.response.status_code}")
                continue
            except httpx.HTTPError as exc:
                reasons.append(f"session {session.get('session_key')}: {type(exc).__name__}")
                continue
            if outline is not None:
                break
            reasons.append(f"session {session.get('session_key')}: no clean lap with location data")
        if outline is None:
            report(f"skip {label}: " + "; ".join(reasons))
            continue
        path = out_dir / f"{key}.json"
        path.write_text(json.dumps(outline, separators=(",", ":")) + "\n", encoding="utf-8")
        written.append(path)
        report(f"wrote {path.name}: {len(outline['points'])} points from session {outline['source_session_key']}")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate circuit outlines from OpenF1 location data.")
    parser.add_argument("--year", type=int, default=datetime.now(timezone.utc).year)
    parser.add_argument("--circuit", type=int, default=None, help="only this circuit_key")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as client:
        written = generate(client, args.year, args.out, circuit=args.circuit)
    print(f"{len(written)} outline(s) written")
    return 0 if written else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
