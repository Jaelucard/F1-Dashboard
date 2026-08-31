"""The contract every data source implements.

Live, replay and historical sources all answer the same questions, so the rest
of the backend never branches on where data came from. ``since`` filters by the
record's own timestamp (OpenF1's ``date`` / ``date_start``), not by when we
received it, so the same call means the same thing in every mode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from app.models import SessionState

Record = dict[str, Any]


class SessionDataSource(ABC):
    """One session's worth of timing data, however it is obtained."""

    @abstractmethod
    def get_drivers(self) -> list[Record]:
        """The entry list, including team_name and team_colour."""

    @abstractmethod
    def get_locations(self, since: datetime | None = None) -> list[Record]:
        """Car x/y/z. May be empty: it does not always stream live."""

    @abstractmethod
    def get_positions(self, since: datetime | None = None) -> list[Record]:
        """Running order."""

    @abstractmethod
    def get_intervals(self, since: datetime | None = None) -> list[Record]:
        """Gap to leader and interval to the car ahead. Races only."""

    @abstractmethod
    def get_laps(self, since: datetime | None = None) -> list[Record]:
        """Lap and sector times."""

    @abstractmethod
    def get_stints(self) -> list[Record]:
        """Tyre stints, for compound and age."""

    @abstractmethod
    def get_pits(self, since: datetime | None = None) -> list[Record]:
        """Pit stops."""

    @abstractmethod
    def get_team_radio(self, since: datetime | None = None) -> list[Record]:
        """Radio clips. Often empty in 2026; that is not an error."""

    @abstractmethod
    def get_race_control(self, since: datetime | None = None) -> list[Record]:
        """Flags, safety car, session status."""

    @abstractmethod
    def get_car_data(self, since: datetime | None = None) -> list[Record]:
        """Speed, gear, throttle, brake, and the opaque legacy `drs` integer."""

    @abstractmethod
    def snapshot(self) -> SessionState:
        """Merge everything above into the one shape the browser consumes."""
