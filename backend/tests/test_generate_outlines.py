"""generate_outlines.py against a fake OpenF1, never the network."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from scripts.generate_outlines import (
    MAX_POINTS,
    fastest_clean_lap,
    generate,
    latest_completed_sessions,
    simplify,
)

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)

SESSIONS = [
    {"session_key": 100, "circuit_key": 39, "circuit_short_name": "Monza", "session_type": "Practice",
     "date_end": "2026-09-04T12:30:00+00:00"},  # in the future: ignored
    {"session_key": 90, "circuit_key": 55, "circuit_short_name": "Zandvoort", "session_type": "Practice",
     "date_end": "2026-08-21T13:00:00+00:00"},
    {"session_key": 91, "circuit_key": 55, "circuit_short_name": "Zandvoort", "session_type": "Race",
     "date_end": "2026-08-23T15:00:00+00:00"},
    {"session_key": 80, "circuit_key": 7, "circuit_short_name": "Spa-Francorchamps", "session_type": "Race",
     "date_end": "2026-07-26T15:00:00+00:00"},
]

LAPS = [
    {"driver_number": 1, "lap_number": 1, "lap_duration": 70.0, "is_pit_out_lap": True, "date_start": "2026-08-23T13:00:00+00:00",
     "duration_sector_1": 20.0, "duration_sector_2": 25.0, "duration_sector_3": 25.0},
    {"driver_number": 4, "lap_number": 2, "lap_duration": 72.5, "is_pit_out_lap": False, "date_start": "2026-08-23T13:01:10+00:00",
     "duration_sector_1": 21.0, "duration_sector_2": 26.0, "duration_sector_3": 25.5},
    {"driver_number": 4, "lap_number": 3, "lap_duration": 71.0, "is_pit_out_lap": False, "date_start": "2026-08-23T13:02:22.5+00:00",
     "duration_sector_1": 21.0, "duration_sector_2": None, "duration_sector_3": 25.0},  # incomplete sectors
    {"driver_number": 16, "lap_number": 3, "lap_duration": 71.9, "is_pit_out_lap": False, "date_start": "2026-08-23T13:02:20+00:00",
     "duration_sector_1": 21.0, "duration_sector_2": 25.4, "duration_sector_3": 25.5},
]


def circle(points: int, radius: float = 1000.0) -> list[dict[str, Any]]:
    return [{"x": radius * math.cos(2 * math.pi * i / points), "y": radius * math.sin(2 * math.pi * i / points),
             "date": "2026-08-23T13:02:20+00:00"} for i in range(points)]


def fake_api(location_rows: dict[int, list[dict[str, Any]]], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = parse_qs(request.url.query.decode())
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json=SESSIONS)
        if request.url.path == "/v1/laps":
            return httpx.Response(200, json=LAPS)
        if request.url.path == "/v1/location":
            key = int(params["session_key"][0])
            return httpx.Response(200, json=location_rows.get(key, []))
        return httpx.Response(404, json={"detail": "No results found."})

    return httpx.Client(base_url="https://api.openf1.org", transport=httpx.MockTransport(handler))


def test_latest_completed_sessions_prefers_a_race_and_ignores_the_future() -> None:
    chosen = latest_completed_sessions(SESSIONS, NOW)
    assert set(chosen) == {55, 7}
    assert chosen[55]["session_key"] == 91


def test_fastest_clean_lap_skips_pit_out_and_incomplete_laps() -> None:
    lap = fastest_clean_lap(LAPS)
    assert lap is not None and (lap["driver_number"], lap["lap_number"]) == (16, 3)
    assert fastest_clean_lap([]) is None


def test_simplify_keeps_endpoints_and_stays_under_the_cap() -> None:
    points = [(p["x"], p["y"]) for p in circle(3000)]
    result = simplify(points)
    assert len(result) <= MAX_POINTS
    assert result[0] == points[0] and result[-1] == points[-1]
    assert simplify(points[:50]) == points[:50], "already small: untouched"


def test_generate_writes_one_file_per_circuit_with_the_lap_window(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    with fake_api({91: circle(600), 80: circle(120)}, seen) as client:
        written = generate(client, 2026, tmp_path, sleep=lambda _: None, now=NOW, report=lambda _: None)

    assert sorted(p.name for p in written) == ["55.json", "7.json"]
    outline = json.loads((tmp_path / "55.json").read_text())
    assert outline["circuit_key"] == 55 and outline["circuit_short_name"] == "Zandvoort"
    assert outline["source_session_key"] == 91
    assert 10 <= len(outline["points"]) <= MAX_POINTS
    assert all(len(p) == 2 for p in outline["points"])

    location = [r for r in seen if r.url.path == "/v1/location" and b"session_key=91" in r.url.query][0]
    params = parse_qs(location.url.query.decode())
    assert params["driver_number"] == ["16"]
    assert params["date>"] == ["2026-08-23T13:02:20+00:00"]
    assert params["date<"] == ["2026-08-23T13:03:31.900000+00:00"]


def test_generate_falls_back_to_the_next_session_when_the_race_has_no_data(tmp_path: Path) -> None:
    """OpenF1 answers 404 for a session it holds nothing for; try the practice."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = parse_qs(request.url.query.decode())
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json=SESSIONS)
        if params.get("session_key") == ["91"]:
            return httpx.Response(404, json={"detail": "No results found."})
        if request.url.path == "/v1/laps":
            return httpx.Response(200, json=LAPS)
        return httpx.Response(200, json=circle(200))

    with httpx.Client(base_url="https://api.openf1.org", transport=httpx.MockTransport(handler)) as client:
        written = generate(client, 2026, tmp_path, circuit=55, sleep=lambda _: None, now=NOW, report=lambda _: None)
    assert [p.name for p in written] == ["55.json"]
    assert json.loads((tmp_path / "55.json").read_text())["source_session_key"] == 90


def test_generate_skips_a_circuit_without_location_data(tmp_path: Path) -> None:
    reports: list[str] = []
    with fake_api({91: circle(300)}, []) as client:
        written = generate(client, 2026, tmp_path, sleep=lambda _: None, now=NOW, report=reports.append)
    assert [p.name for p in written] == ["55.json"]
    assert any(line.startswith("skip 7 Spa") for line in reports)


def test_generate_can_be_restricted_to_one_circuit(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    with fake_api({91: circle(300), 80: circle(300)}, seen) as client:
        written = generate(client, 2026, tmp_path, circuit=7, sleep=lambda _: None, now=NOW, report=lambda _: None)
    assert [p.name for p in written] == ["7.json"]
    assert not any(b"session_key=91" in r.url.query for r in seen)


def test_generator_never_reads_the_backend_env() -> None:
    import scripts.generate_outlines as module

    source = Path(module.__file__).read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any("app" in line.split()[1].split(".")[0] for line in imports), imports
