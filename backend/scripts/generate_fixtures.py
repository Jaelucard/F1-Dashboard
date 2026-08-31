"""Generate the frontend's test fixture from the real backend adapter.

The Phase 2 acceptance test is that the leaderboard renders 20-plus drivers with
the right columns, team colours and the updated lap value. Hand-writing that
fixture in TypeScript would test the frontend against a guess about the backend.
Instead the fixture is produced by feeding sample messages through the actual
``OpenF1LiveSource`` and serialising the resulting ``SessionState`` - so the
frontend test renders exactly the bytes the backend would send.

``server_time`` and the recorder block are pinned to constants; otherwise the
fixture would differ on every run and the freshness check could never pass.

Usage:  .venv/bin/python -m scripts.generate_fixtures [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import RecorderInfo  # noqa: E402
from scripts.sample_data import build_source  # noqa: E402

OUTPUT = (
    Path(__file__).resolve().parent.parent.parent
    / "frontend"
    / "src"
    / "test"
    / "sampleSnapshot.json"
)

PINNED_SERVER_TIME = "2026-09-04T11:46:05.000+00:00"


def generate() -> str:
    state = build_source().snapshot(
        mode="live",
        recorder=RecorderInfo(
            connected=True,
            messages_recorded=48231,
            last_message_at="2026-09-04T11:46:04.812+00:00",
            topics={"v1/laps": 1204, "v1/intervals": 8801, "v1/car_data": 30112},
            token_expires_at="2026-09-04T12:30:00+00:00",
        ),
    )
    state.server_time = PINNED_SERVER_TIME
    return json.dumps(state.model_dump(), indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    generated = generate()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != generated:
            print(f"{OUTPUT} is stale. Run: make types", file=sys.stderr)
            return 1
        print(f"{OUTPUT.name} is up to date")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated)
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
