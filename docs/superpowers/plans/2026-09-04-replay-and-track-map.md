# Replay and Track Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay a recorded session through the existing snapshot path with transport controls, and draw a track map with bundled circuit outlines.

**Architecture:** A `ReplayPlayer` thread streams `recordings/<session_key>/*.jsonl` in `received_at` order into a fresh `OpenF1LiveSource`, so replay snapshots reuse all live validation and merging. A small `/replay` HTTP API controls it and the snapshot carries a `replay` block. The frontend gains a `TrackMap` (SVG, bundled outline JSON generated from OpenF1 history, fallback trace) and a `ReplayBar`.

**Tech Stack:** Python 3.13, FastAPI, pydantic v2, httpx, pytest; React 19, zustand, Tailwind v4, vitest + testing-library.

**Spec:** `docs/superpowers/specs/2026-09-03-replay-and-track-map-design.md`

## Global Constraints

- Backend pinned to Python 3.13; run everything through `backend/.venv/bin/python`.
- Every pytest run sets `LIVE_MODE=false OPENF1_USERNAME= OPENF1_PASSWORD=` (conftest does this too).
- No absolute filesystem path may appear in `/health`, `/replay*`, snapshots, errors or logs.
- Test credentials must be the synthetic placeholders already in use (`test-user@example.invalid`, `test-password-placeholder`).
- Ruff defaults must pass: `cd backend && .venv/bin/ruff check .`.
- After any model change run `make types` and commit the regenerated `frontend/src/types/sessionState.ts` and `frontend/src/test/sampleSnapshot.json`.
- Commits use the repo-local noreply identity (already configured) and end with the Claude attribution trailer.
- Speed range `[0.25, 50]`, gap clamp `[0, 5 s]`, session key `0 <= key < 10**12`.

---

### Task 1: Models

**Files:**
- Modify: `backend/app/models.py`
- Test: `backend/tests/test_generated_types.py` (existing), `backend/tests/test_models.py` (new)

**Interfaces:**
- Produces: `ReplayState = Literal["loaded","playing","paused","seeking","finished"]`, `class ReplayInfo(Base)` with fields `session_key:int, state:ReplayState, speed:float, position:str|None, start:str|None, end:str|None, progress:float, messages_replayed:int, skipped_lines:int, ingest_errors:int`; `DriverState.x: float|None`, `DriverState.y: float|None`, `DriverState.location_at: str|None`; `SessionState.replay: ReplayInfo|None = None`.

- [ ] Write `tests/test_models.py`:

```python
from app.models import DriverState, ReplayInfo, SessionState

def test_replay_info_round_trips() -> None:
    info = ReplayInfo(session_key=42, state="paused", speed=2.0, position=None, start=None, end=None,
                      progress=0.0, messages_replayed=0, skipped_lines=0, ingest_errors=0)
    assert ReplayInfo.model_validate(info.model_dump()) == info

def test_driver_state_carries_optional_location() -> None:
    d = DriverState(driver_number=1)
    assert d.x is None and d.y is None and d.location_at is None

def test_session_state_replay_block_defaults_to_none() -> None:
    assert "replay" in SessionState.model_fields
```

- [ ] Run: `pytest tests/test_models.py -q` → FAIL (ImportError).
- [ ] Add the models per the interface block, with docstrings so they reach the generated TS.
- [ ] Run `make types`, then `pytest -q` → all pass (staleness tests regenerate).
- [ ] Commit: `feat(models): replay info and driver location fields`.

### Task 2: Adapter fills driver location

**Files:**
- Modify: `backend/app/adapters/openf1_live.py` (`_build_drivers`, `_build_driver`)
- Test: `backend/tests/test_live_adapter.py`

**Interfaces:**
- Consumes: `LatestPerDriverStore.by_driver()` for `TOPIC_LOCATION`.
- Produces: `DriverState.x/y/location_at` populated from the latest location sample.

- [ ] Test:

```python
def test_driver_state_carries_latest_location() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/drivers", {"driver_number": 1, "session_key": 5})
    source.ingest("v1/laps", {"driver_number": 1, "lap_number": 1, "session_key": 5, "date_start": "2026-09-04T11:00:00+00:00"})
    source.ingest("v1/location", {"driver_number": 1, "x": 1.0, "y": 2.0, "z": 3.0, "date": "2026-09-04T11:00:01+00:00", "session_key": 5})
    source.ingest("v1/location", {"driver_number": 1, "x": 10.0, "y": 20.0, "z": 3.0, "date": "2026-09-04T11:00:02+00:00", "session_key": 5})
    driver = source.snapshot().drivers[0]
    assert (driver.x, driver.y, driver.location_at) == (10.0, 20.0, "2026-09-04T11:00:02+00:00")

def test_driver_without_location_has_no_coordinates() -> None:
    ...x is None...
```

- [ ] Implement: `locations = self._by_driver(TOPIC_LOCATION)` in `_build_drivers`, pass `locations.get(number, {})` as a new `location: Record` parameter of `_build_driver`, set `x=location.get("x"), y=location.get("y"), location_at=location.get("date")`.
- [ ] Run tests, `make types` (fixture unchanged until Task 3), commit: `feat(adapter): expose latest car location on DriverState`.

### Task 3: Sample data with locations

**Files:**
- Modify: `backend/scripts/sample_data.py`
- Regenerate: `frontend/src/test/sampleSnapshot.json`
- Test: `backend/tests/test_generated_types.py`, `frontend/src/components/Leaderboard.test.tsx` (unchanged, must pass)

- [ ] Add `build_loop(points: int = 400) -> list[tuple[float, float]]` (rounded rectangle 6000 x 3000 with 800 radius corners) and, in `build_messages()`, one `v1/location` message per driver except the pitted driver, at `loop[(index * 17) % 400]`, `date` = `2026-09-04T11:45:{03 + 0.27*index}`. Remove `circuit_key` from the sample session.
- [ ] Run `make types`; `pytest -q`; `cd frontend && npm test`. Commit: `feat(sample): synthetic car locations`.

### Task 4: Recording reader

**Files:**
- Create: `backend/app/replay.py`
- Test: `backend/tests/test_replay.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class ReplayMessage: received_at: datetime; topic: str; payload: Any`; `def parse_line(line: str) -> ReplayMessage | None`; `class RecordingReader` with `__iter__/__next__`, `seek(to: datetime) -> None`, `close()`, attribute `skipped_lines: int`.

- [ ] Test helper writing recordings with `RawMessage(...).to_line()` into `tmp_path/<key>/<topic>.jsonl`; tests: merge order across three topic files; interleaved timestamps; malformed line skipped and counted; non-object JSON skipped; `seek` skips earlier lines and yields the rest; iterating an empty directory yields nothing.
- [ ] Implement with `heapq` of `(received_at, file_index, counter, message)`.
- [ ] Commit: `feat(replay): streaming recording reader`.

### Task 5: Recording catalog

**Files:** `backend/app/replay.py`, `backend/tests/test_replay.py`

**Interfaces:**
- Produces: `class RecordingSummary(BaseModel)`: `session_key:int, topics:dict[str,int], size_bytes:int, start:str|None, end:str|None, session_name:str|None, circuit_short_name:str|None`; `class RecordingCatalog` with `sessions() -> list[RecordingSummary]` (sorted by key desc) and `summary(session_key:int) -> RecordingSummary` (raises `ReplayError`); `def resolve_session_dir(root: Path, session_key: int) -> Path` (raises `ReplayError("unknown session")`).

- [ ] Tests: missing root → `[]`; non-digit directories ignored; start/end from first/last lines across files; session name from `v1_sessions.jsonl`; `resolve_session_dir` rejects negative, oversize, and a symlink pointing outside root; error message contains no path.
- [ ] Commit: `feat(replay): recording catalog and path safety`.

### Task 6: Replay player

**Files:** `backend/app/replay.py`, `backend/tests/test_replay.py`

**Interfaces:**
- Produces: `class ReplayPlayer(settings, session_key, *, sleep: Callable[[float], None] | None = None, adapter_factory=...)` with `play()`, `pause()`, `set_speed(float)`, `seek(*, position: datetime|None=None, fraction: float|None=None)`, `stop()`, `info() -> ReplayInfo`, `snapshot() -> SessionState`, properties `state`, `speed`, `position`.
- Pacing sleep goes through `self._wait(seconds)`, which uses the condition variable so pause/seek/speed wake it; tests inject `sleep` to record durations.

- [ ] Tests (each uses `wait_for` polling with a 5 s cap): loaded state after construction with progress 0; play applies messages and reaches `finished`; recorded sleeps equal gap/speed and are clamped at 5 s; pause stops progress and resume continues; `set_speed` outside range raises `ValueError`; forward seek by fraction applies without sleeping; backward seek yields adapter state equal to a fresh run to the same point; `play()` after `finished` restarts; ingest exception counted; snapshot mode is `replay` with a `replay` block and `recorder is None`; `stop()` joins.
- [ ] Commit: `feat(replay): player thread with pacing, seek and speed`.

### Task 7: Control API and wiring

**Files:**
- Create: `backend/app/replay_api.py`
- Modify: `backend/app/ws.py` (extract `presented_token(query_token, authorization_header) -> str|None` and `token_matches(presented, expected) -> bool`), `backend/app/main.py`
- Test: `backend/tests/test_replay_api.py`, extend `backend/tests/test_api.py::test_health_and_startup_logs_never_expose_the_recordings_path`

- [ ] Tests: `GET /replay/sessions` lists a temp recording; `GET /replay` idle; load → 200 with state `loaded` and snapshot mode `replay` over the WebSocket; unknown key 404; play/pause/speed/seek/unload transitions; 409 when `app.state.recorder` is set or `app.state.demo` is True; token required on POST when `WS_AUTH_TOKEN` set (401 without, 200 with bearer and with `?token=`), GET open; `/health` carries `replay` and contains neither `recordings_dir` nor the path.
- [ ] Implement router with `prefix="/replay"`, dependencies `require_available` and `require_token`, bodies `LoadBody(session_key: int = Field(ge=0, lt=10**12))`, `SeekBody(position: str|None, fraction: float|None = Field(default=None, ge=0, le=1))` with a model validator requiring exactly one, `SpeedBody(speed: float = Field(ge=0.25, le=50))`. Player stored on `app.state.replay`. `main.build_snapshot` returns the player's snapshot when loaded; lifespan stops the player on shutdown; CORS allows POST.
- [ ] Commit: `feat(api): replay control endpoints`.

### Task 8: Outline generator

**Files:**
- Create: `backend/scripts/generate_outlines.py`, `frontend/src/data/circuits/*.json` (generated), `frontend/src/data/circuits/README.md`
- Modify: `Makefile` (`outlines` target + help)
- Test: `backend/tests/test_generate_outlines.py`

**Interfaces:**
- Produces: `fastest_clean_lap(laps: list[dict]) -> dict|None`, `simplify(points: list[tuple[float,float]], max_points: int = 400) -> list[tuple[float,float]]` (Douglas-Peucker, epsilon doubled until under the cap), `latest_completed_sessions(sessions, now) -> dict[int, dict]`, `build_outline(client, session, sleep) -> dict|None`, `generate(client, year, out_dir, circuit=None, sleep=time.sleep, now=None) -> list[Path]`, `main(argv) -> int`.

- [ ] Tests with `httpx.MockTransport`: picks the fastest lap with all sectors and not pit-out; windows the location request by `date_start` and `lap_duration`; simplify keeps endpoints and stays under the cap; a circuit whose location window is empty is skipped; `--circuit` restricts; output file schema.
- [ ] Run `make outlines` once against the real API and commit the JSON files.
- [ ] Commit: `feat(map): circuit outline generator and bundled outlines`.

### Task 9: Frontend token helper and replay API client

**Files:**
- Create: `frontend/src/lib/token.ts`, `frontend/src/lib/replayApi.ts`, `frontend/src/lib/replayApi.test.ts`
- Modify: `frontend/src/lib/useSessionSocket.ts` (import `accessToken` from `./token`)

**Interfaces:**
- Produces: `accessToken(): string | null`; `type ApiResult<T> = { ok: true; value: T } | { ok: false; error: string }`; `listSessions()`, `getReplay()`, `loadSession(key)`, `play()`, `pause()`, `seekFraction(f)`, `setSpeed(s)`, `unload()` all returning `Promise<ApiResult<...>>`; `RecordingSummary` type.

- [ ] Tests with `vi.stubGlobal('fetch', ...)`: endpoints and methods; bearer header present when a token is stored; non-2xx becomes `{ok:false, error: detail}`; network failure becomes `{ok:false}` not a throw.
- [ ] Commit: `feat(frontend): replay API client`.

### Task 10: Track map

**Files:**
- Create: `frontend/src/data/circuits/index.ts`, `frontend/src/lib/useTrackTrace.ts`, `frontend/src/components/TrackMap.tsx`, tests for each.

**Interfaces:**
- `loadOutline(circuitKey: number | null | undefined): Promise<Outline | null>` where `Outline = { circuit_key: number; circuit_short_name: string; points: [number, number][] }`.
- `useTrackTrace(drivers): [number, number][]` ring of 4000 points, reset when `session_key` changes.
- `TrackMap` renders `<svg data-testid="track-map">`, `<path data-testid="outline">`, and `<g data-driver="N">` per driver with coordinates.

- [ ] Tests: outline path present for a known circuit (inject a test outline via `loadOutline` mock); fallback trace grows across snapshots; driver group count excludes drivers without x/y; pitted driver has `data-in-pit="true"`; empty state text when nothing to draw.
- [ ] Commit: `feat(frontend): track map`.

### Task 11: Replay bar

**Files:** `frontend/src/components/ReplayBar.tsx`, `frontend/src/components/ReplayBar.test.tsx`

- [ ] Tests: hidden for `live`/`demo`; idle shows picker from mocked `listSessions` and "No recordings found" when empty; Load calls `loadSession`; replay mode shows play/pause, speed select, range bound to `progress`; slider change calls `seekFraction`; error message shown when a call fails.
- [ ] Commit: `feat(frontend): replay transport bar`.

### Task 12: Layout

**Files:** `frontend/src/App.tsx`, `frontend/src/App.test.tsx`

- [ ] Test: renders StatusStrip, Leaderboard, TrackMap; ReplayBar present in idle.
- [ ] Implement the two-column layout (`lg:flex-row`, map `lg:w-[26rem]`, `h-56` stacked below lg).
- [ ] Commit: `feat(frontend): layout with track map column`.

### Task 13: Docs and phase bump

**Files:** `README.md`, `backend/app/__init__.py` (`PHASE = 4`), `backend/tests/test_api.py` (phase asserts), `Makefile` help.

- [ ] Update, run `make types` (phase is in the fixture), full backend + frontend tests, `make lint`.
- [ ] Commit: `docs: replay and track map, phase 4`.

### Task 14: Verification

- [ ] `cd backend && .venv/bin/python -m pytest -q`; `cd frontend && npm test`; `make lint`.
- [ ] Sweep tracked files for absolute paths and credentials (same grep as the hygiene pass).
- [ ] Report.
