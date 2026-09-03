"""A synthetic but realistically-shaped session, for tests and fixtures.

The driver names and numbers here are invented; only the team names are real,
because the fallback colour map in teams.ts is keyed on them. Payload *shapes*
mirror what the live API actually returns, including the awkward cases:

* a lapped car whose ``gap_to_leader`` is the string "+1 LAP"
* drivers whose ``team_colour`` is missing, exercising the teams.ts fallback
* a lap delivered twice under the same ``_key``, first with sector 1 only and
  then revised with the full lap - the upsert case the docs call out
* a car in the pits, and a car on an out-lap
* ``aero_raw`` values present but meaningless, as in 2026
"""

from __future__ import annotations

from typing import Any

TEAMS_2026: list[tuple[str, str | None]] = [
    ("Red Bull Racing", "4781D7"),
    ("Ferrari", "ED1131"),
    ("Mercedes", "00D7B6"),
    ("McLaren", "F47600"),
    ("Aston Martin", "229971"),
    ("Alpine", "00A1E8"),
    ("Williams", "1868DB"),
    ("Racing Bulls", "6C98FF"),
    ("Audi", None),      # no colour from the API -> teams.ts fallback
    ("Haas", "9C9FA2"),
    ("Cadillac", None),  # no colour and, for one driver, no team either
]

ACRONYMS = [
    "VER", "LAW", "LEC", "HAM", "RUS", "ANT", "NOR", "PIA", "ALO", "STR",
    "GAS", "COL", "ALB", "SAI", "TSU", "HAD", "HUL", "BOR", "OCO", "BEA",
    "PER", "BOT",
]


def build_grid() -> list[dict[str, Any]]:
    """22 drivers across the 11 teams of the 2026 grid."""
    drivers = []
    for index, acronym in enumerate(ACRONYMS):
        team_name, colour = TEAMS_2026[index // 2]
        drivers.append(
            {
                "driver_number": 100 + index,
                "name_acronym": acronym,
                "full_name": f"{acronym.title()} Driver",
                "broadcast_name": f"X {acronym}",
                "team_name": team_name,
                "team_colour": colour,
                "session_key": 9999,
                "meeting_key": 1300,
            }
        )
    return drivers


def build_loop(points: int = 400) -> list[tuple[float, float]]:
    """A closed loop in OpenF1-like coordinates: a rounded rectangle.

    Used for the synthetic ``v1/location`` samples so the track map has cars
    to draw outside a session window. Not any real circuit.
    """
    import math

    width, height, radius = 6000.0, 3000.0, 800.0
    straight_w = width - 2 * radius
    straight_h = height - 2 * radius
    arc = math.pi * radius / 2
    perimeter = 2 * straight_w + 2 * straight_h + 4 * arc
    out: list[tuple[float, float]] = []
    for i in range(points):
        d = perimeter * i / points
        if d < straight_w:                       # bottom edge, left to right
            x, y = radius + d, 0.0
        elif d < straight_w + arc:               # bottom-right corner
            a = (d - straight_w) / radius
            x, y = width - radius + radius * math.sin(a), radius - radius * math.cos(a)
        elif d < straight_w + arc + straight_h:  # right edge, upwards
            x, y = width, radius + (d - straight_w - arc)
        elif d < straight_w + 2 * arc + straight_h:
            a = (d - straight_w - arc - straight_h) / radius
            x, y = width - radius + radius * math.cos(a), height - radius + radius * math.sin(a)
        elif d < 2 * straight_w + 2 * arc + straight_h:  # top edge, right to left
            x, y = width - radius - (d - straight_w - 2 * arc - straight_h), height
        elif d < 2 * straight_w + 3 * arc + straight_h:
            a = (d - 2 * straight_w - 2 * arc - straight_h) / radius
            x, y = radius - radius * math.sin(a), height - radius + radius * math.cos(a)
        elif d < 2 * straight_w + 3 * arc + 2 * straight_h:  # left edge, downwards
            x, y = 0.0, height - radius - (d - 2 * straight_w - 3 * arc - straight_h)
        else:
            a = (d - 2 * straight_w - 3 * arc - 2 * straight_h) / radius
            x, y = radius - radius * math.cos(a), radius - radius * math.sin(a)
        out.append((round(x - width / 2, 1), round(y - height / 2, 1)))
    return out


def build_messages() -> list[tuple[str, dict[str, Any]]]:
    """Every message the injector feeds, in arrival order."""
    messages: list[tuple[str, dict[str, Any]]] = [
        (
            "v1/sessions",
            {
                "session_key": 9999,
                "meeting_key": 1300,
                # No circuit_key on purpose: the sample session has no bundled
                # track outline, so demo mode exercises the fallback trace.
                "session_name": "Practice 1",
                "session_type": "Practice",
                "circuit_short_name": "Monza",
                "country_name": "Italy",
                "location": "Monza",
                "year": 2026,
                "date_start": "2026-09-04T11:30:00+00:00",
                "date_end": "2026-09-04T12:30:00+00:00",
                "gmt_offset": "02:00:00",
            },
        )
    ]

    grid = build_grid()
    for driver in grid:
        messages.append(("v1/drivers", driver))

    base_lap = 79.5
    for position, driver in enumerate(grid, start=1):
        number = driver["driver_number"]
        lap_time = round(base_lap + position * 0.181, 3)

        messages.append(
            ("v1/position", {"driver_number": number, "position": position,
                             "date": "2026-09-04T11:45:00+00:00", "session_key": 9999})
        )

        # The lapped backmarker: OpenF1 really does send this as a string.
        gap: float | str = "+1 LAP" if position == len(grid) else round((position - 1) * 0.34, 3)
        messages.append(
            ("v1/intervals", {"driver_number": number, "gap_to_leader": gap,
                              "interval": None if position == 1 else 0.34,
                              "date": "2026-09-04T11:45:02+00:00", "session_key": 9999})
        )

        # An earlier, slower lap so that "best" differs from "last".
        messages.append(
            ("v1/laps", {"_key": f"lap-{number}-11", "_id": 1000 + number,
                         "driver_number": number, "lap_number": 11,
                         "lap_duration": round(lap_time + 0.9, 3),
                         "duration_sector_1": 28.0, "duration_sector_2": 26.0,
                         "duration_sector_3": 25.5, "is_pit_out_lap": False,
                         "date_start": "2026-09-04T11:40:00+00:00", "session_key": 9999})
        )

        # THE UPSERT CASE: lap 12 arrives twice under one _key. First only
        # sector 1 is known; the revision completes it. Appending instead of
        # upserting would leave two partial copies of the same lap.
        messages.append(
            ("v1/laps", {"_key": f"lap-{number}-12", "_id": 2000 + number,
                         "driver_number": number, "lap_number": 12,
                         "duration_sector_1": 27.8, "lap_duration": None,
                         "is_pit_out_lap": False,
                         "date_start": "2026-09-04T11:44:00+00:00", "session_key": 9999})
        )
        messages.append(
            ("v1/laps", {"_key": f"lap-{number}-12", "_id": 3000 + number,
                         "driver_number": number, "lap_number": 12,
                         "duration_sector_2": 25.9, "duration_sector_3": 25.4,
                         "lap_duration": lap_time, "session_key": 9999})
        )

        # A few drivers post a slower lap afterwards, so their LAST is not
        # their BEST and the yellow timing colour gets exercised.
        if position % 6 == 0:
            messages.append(
                ("v1/laps", {"_key": f"lap-{number}-13", "_id": 5000 + number,
                             "driver_number": number, "lap_number": 13,
                             "lap_duration": round(lap_time + 1.7, 3),
                             "duration_sector_1": 28.4, "duration_sector_2": 26.3,
                             "duration_sector_3": 25.9, "is_pit_out_lap": False,
                             "date_start": "2026-09-04T11:46:00+00:00", "session_key": 9999})
            )

        compound = ["SOFT", "MEDIUM", "HARD"][position % 3]
        messages.append(
            ("v1/stints", {"driver_number": number, "stint_number": 1,
                           "lap_start": 5, "lap_end": 20, "compound": compound,
                           "tyre_age_at_start": 2 if position % 4 == 0 else 0,
                           "session_key": 9999})
        )
        messages.append(
            ("v1/car_data", {"driver_number": number, "speed": 300 + position,
                             "n_gear": 7, "throttle": 100, "brake": 0,
                             # Opaque in 2026. Recorded, never interpreted.
                             "drs": 8 if position % 5 == 0 else 0,
                             "date": "2026-09-04T11:45:03+00:00", "session_key": 9999})
        )

    # Car positions around the synthetic loop, spread out by grid slot. The
    # pitted car (grid[3]) sends none, so the map has a driver without a dot.
    loop = build_loop()
    for index, driver in enumerate(grid):
        if index == 3:
            continue
        x, y = loop[(index * 17) % len(loop)]
        stamp = 3 + 0.27 * index
        messages.append(
            ("v1/location", {"driver_number": driver["driver_number"], "x": x, "y": y, "z": 0,
                             "date": f"2026-09-04T11:45:{stamp:06.3f}+00:00", "session_key": 9999})
        )

    # One car in the pits this lap, one on an out-lap.
    pit_driver = grid[3]["driver_number"]
    messages.append(
        ("v1/pit", {"driver_number": pit_driver, "lap_number": 12,
                    "date": "2026-09-04T11:44:30+00:00", "pit_duration": 23.6,
                    "lane_duration": 23.6, "stop_duration": 2.1, "session_key": 9999})
    )
    out_driver = grid[7]["driver_number"]
    messages.append(
        ("v1/laps", {"_key": f"lap-{out_driver}-12", "_id": 4000,
                     "driver_number": out_driver, "lap_number": 12,
                     "is_pit_out_lap": True, "session_key": 9999})
    )

    messages.append(
        ("v1/race_control", {"date": "2026-09-04T11:30:00+00:00", "category": "Flag",
                             "flag": "GREEN", "scope": "Track",
                             "message": "GREEN LIGHT - PIT EXIT OPEN", "session_key": 9999})
    )
    messages.append(
        ("v1/race_control", {"date": "2026-09-04T11:46:00+00:00", "category": "Flag",
                             "flag": "YELLOW", "scope": "Track", "sector": 3,
                             "message": "YELLOW IN TRACK SECTOR 3", "session_key": 9999})
    )
    return messages


def build_source():  # type: ignore[no-untyped-def]
    """A live source with every sample message applied."""
    from app.adapters.openf1_live import OpenF1LiveSource

    source = OpenF1LiveSource(credentials_present=True)
    for topic, payload in build_messages():
        source.ingest(topic, payload)
    return source
