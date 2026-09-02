# Phase 3 + 4: Replay and Track Map

Date: 2026-09-03
Status: approved design, awaiting implementation plan

## Goal

Phase 3: replay a recorded session (`recordings/<session_key>/<topic>.jsonl`)
through the existing snapshot and WebSocket path, with a session picker and
transport controls (play, pause, seek, speed).

Phase 4: a track map panel that draws a bundled circuit outline and the live
position of every car, in live, demo and replay modes.

Out of scope: 2026 aero mapping, team radio, the historical REST adapter
(Phase 5), and checkpointed seeking.

## Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Replay engine | Re-feed the existing `OpenF1LiveSource` | Snapshots share validation, merging and session scoping with live mode; nothing to drift. |
| Replay control | Session picker + transport bar in the UI over a small HTTP API | Needed to analyse a capture; env-only replay would require restarts. |
| Track outline | Bundled per-circuit JSON generated from OpenF1's own historical `location` data | Same coordinate frame as live data, so no calibration; attribution stays OpenF1. |
| Outline fallback | Trace accumulated car positions in the browser | Covers circuits with no bundled file (Madring until its first session is historical). |
| Timing basis for replay | The recorder's `received_at`, not payload `date` | Uniform across topics and is exactly what the live adapter experienced. |

## Backend

### `app/replay.py`

Three units, each testable alone.

**`RecordingCatalog(root: Path)`**

- `sessions() -> list[RecordingSummary]`: one entry per child directory of
  `root` whose name is all digits. Each summary carries `session_key` (int),
  `topics` (a map of topic name to file size in bytes; line counts are not
  computed), `start` and `end` (earliest first-line and latest last-line
  `received_at` across topic files), `size_bytes`, and `session_name` /
  `circuit_short_name` when a `v1_sessions.jsonl` file exists and its last
  line parses.
- Reads only the first and last line of each file. Never loads a recording.
- A missing root returns an empty list, not an error.

**`RecordingReader(directory: Path)`**

- Iterates `RawMessage`-shaped tuples `(received_at, topic, payload)` across
  all `*.jsonl` files in the directory, merged by `received_at` with a heap.
  Each file is already in write order, so the merge is a k-way merge, and
  memory is one line per open file.
- The topic is recovered from the line's own `topic` field, not the filename
  (filenames are sanitised).
- A line that is not JSON, is not an object, or has no parseable `received_at`
  is skipped and counted in `skipped_lines`.
- `seek(to: datetime)` restarts the files and skips lines before `to`
  without yielding them (used by backward seeks).

**`ReplayPlayer(settings, session_key, *, clock=time.monotonic, sleep=...)`**

- Owns one `OpenF1LiveSource(credentials_present=settings.credentials_present)`
  and one `RecordingReader`. Runs a daemon thread.
- State machine: `loaded -> playing <-> paused`, `seeking` while a seek is in
  progress, `finished` when the reader is exhausted. `play()` on `finished`
  restarts from the start.
- Pacing: between two messages the thread sleeps
  `(next.received_at - prev.received_at) / speed`, clamped to `[0, 5 s]` so a
  gap in the recording (a red flag) never freezes the replay for minutes.
  `speed` is a float in `[0.25, 50]`.
- `position` is the `received_at` of the last message applied; `start` and
  `end` come from the catalog summary; `progress = (position - start) / (end - start)`.
- `seek(to)`: forward = apply messages without sleeping until `to`;
  backward = replace the adapter with a fresh one, `reader.seek(to)`, then
  the same fast-forward. State is `seeking` throughout and `playing`/`paused`
  is restored afterwards. `seek` accepts an absolute time or a fraction.
- Exceptions from `ingest` are caught per message, counted as
  `ingest_errors`, and logged once per 100.
- `snapshot() -> SessionState`: the adapter's snapshot with `mode="replay"`,
  `recorder=None`, `feed=None`, `replay=ReplayInfo(...)`.
- `stop()` joins the thread. Loading another session stops the current
  player first.
- Path safety: `session_key` is a non-negative int whose decimal form is at
  most 12 digits, and the resolved
  directory must be inside `settings.recordings_dir`; otherwise
  `ReplayError("unknown session")`. The absolute path never appears in
  logs, errors or API responses.

### Control API (`app/main.py`, helpers in `app/replay_api.py`)

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/replay/sessions` | | `{"sessions": [RecordingSummary...]}` |
| GET | `/replay` | | current `ReplayInfo` or `{"state": "idle"}` |
| POST | `/replay/load` | `{"session_key": 11353}` (int) | stop current player, start a new one paused at the start |
| POST | `/replay/play` | | |
| POST | `/replay/pause` | | |
| POST | `/replay/seek` | `{"position": ISO}` or `{"fraction": 0..1}` | |
| POST | `/replay/speed` | `{"speed": 5}` | |
| POST | `/replay/unload` | | stop and return to idle |

- Every POST returns the resulting `ReplayInfo`. Unknown session: 404.
  Invalid body: 422 (FastAPI). Replay unavailable: 409 with
  `{"detail": "replay is unavailable while <live recording|demo mode> is active"}`.
- Availability: replay is allowed only when `app.state.recorder is None` and
  `app.state.demo is False`. It never touches the recorder.
- Auth: when `WS_AUTH_TOKEN` is set, POST endpoints require it as
  `Authorization: Bearer <token>` or `?token=`, compared with
  `hmac.compare_digest` via the same helper `ws.py` uses (refactored to a
  shared function; behaviour of `/ws` unchanged). GET endpoints are open,
  like `/health`. CORS `allow_methods` becomes `["GET", "POST"]`.
- `build_snapshot()` in `main.py`: if a player is loaded, return
  `player.snapshot()`; otherwise unchanged.
- `/health` gains `"replay": ReplayInfo | None`. It contains state, times and
  counters only, never a path. The existing no-path test is extended.
- Lifespan shutdown stops any player.

### Models (`app/models.py`)

```python
ReplayState = Literal["loaded", "playing", "paused", "seeking", "finished"]

class ReplayInfo(Base):
    session_key: int
    state: ReplayState
    speed: float
    position: str | None      # received_at of the last applied message
    start: str | None
    end: str | None
    progress: float           # 0..1
    messages_replayed: int
    skipped_lines: int
    ingest_errors: int

class DriverState(Base):
    ...
    x: float | None = None
    y: float | None = None
    location_at: str | None = None
    """``date`` of the location sample the x/y came from."""

class SessionState(Base):
    ...
    replay: ReplayInfo | None = None
```

`Mode` already includes `"replay"`. `make types` regenerates
`frontend/src/types/sessionState.ts` and `frontend/src/test/sampleSnapshot.json`;
the existing staleness test enforces it.

### Sample data (`scripts/sample_data.py`)

`build_messages()` gains `v1/location` samples: every driver placed on a
synthetic closed loop (a rounded rectangle, 400 points), offset by grid slot,
with `date` values 0.27 s apart. `session_key` 9999, `circuit_key` absent, so
the demo exercises the fallback trace. One driver (the one in the pits) gets
no location sample, so the map has a driver without a dot.

### Outline generator (`scripts/generate_outlines.py`, `make outlines`)

- `python -m scripts.generate_outlines --year 2026 [--circuit KEY] [--out DIR]`.
- Uses `httpx` against `https://api.openf1.org` with no credentials. Never
  imports `app.config`, so it cannot read `.env`.
- For each `circuit_key` in `/v1/sessions?year=YEAR` (latest session whose
  `date_end` is in the past, preferring `session_type=Race`): fetch
  `/v1/laps?session_key=K`, pick the lap with the smallest `lap_duration`
  among laps with `is_pit_out_lap == false` and all three sectors present;
  fetch `/v1/location?session_key=K&driver_number=D&date>=date_start&date<date_start+lap_duration`;
  simplify with Douglas-Peucker to at most 400 points; write
  `frontend/src/data/circuits/<circuit_key>.json`.
- Output schema:

```json
{
  "circuit_key": 39,
  "circuit_short_name": "Monza",
  "source_session_key": 11000,
  "generated_at": "2026-09-03T00:00:00+00:00",
  "points": [[x, y], ...]
}
```

- Sleeps to stay under 3 requests per second. A circuit that fails (no
  completed session, empty location window, HTTP error) is reported and
  skipped; the script exits non-zero only if nothing was written.
- Tests inject a fake transport (`httpx.MockTransport`); no test touches the
  network.

## Frontend

### Types and data

- `src/types/sessionState.ts`: regenerated.
- `src/data/circuits/*.json` + `src/data/circuits/index.ts` exporting
  `loadOutline(circuitKey: number): Promise<Outline | null>` using
  `import.meta.glob` so each outline is its own chunk.

### `components/TrackMap.tsx`

- Props: none; subscribes to `selectDrivers` and `selectSession`.
- Resolves the outline for `session.circuit_key`; while loading or absent,
  uses `useTrackTrace()` which appends each driver's `(x, y)` from every
  snapshot into a ring of 4000 points and returns them as the outline.
- SVG with `viewBox` from the outline bounds plus 5% padding,
  `preserveAspectRatio="xMidYMid meet"`. Outline: `<path>` stroke
  `--color-f1-line`, width computed from the bounds so it is ~2 px on screen.
  Cars: `<g transform="translate(x y)">` with a circle in team colour (via
  `teamColour()`), the acronym as text, `opacity 0.4` when `in_pit`,
  `transition: transform 1s linear` so dots glide between snapshots. Drivers
  with no `x`/`y` are not drawn.
- Y is flipped (OpenF1's y grows northwards; SVG's grows downwards).
- Empty state: "Waiting for location data" when no outline and no points.

### `components/ReplayBar.tsx` and `lib/replayApi.ts`

- Rendered by `App` when `snapshot.mode` is `idle` or `replay`.
- Idle: a `<select>` of sessions from `GET /replay/sessions` (fetched on
  mount and on demand via a refresh button) plus a Load button. Empty list
  shows "No recordings found".
- Replay: session label, play/pause, speed `<select>` (1, 2, 5, 10, 25),
  a range input bound to `replay.progress` that seeks by fraction on change,
  position / end time text, Unload.
- `replayApi.ts`: typed wrappers over `fetch` for every endpoint; sends the
  token from `VITE_WS_TOKEN` or `localStorage['f1dash.wsToken']` (the same
  lookup `socketController` uses, factored into `lib/token.ts`). Errors
  surface as a one-line message in the bar, never a thrown exception.

### Layout (`App.tsx`)

```
lg and up:   [ StatusStrip                              ]
             [ Leaderboard (flex-1) | TrackMap (w-[26rem]) ]
             [ ReplayBar (when idle/replay)              ]
             [ footer                                    ]
below lg:    StatusStrip / TrackMap (h-56) / Leaderboard / ReplayBar / footer
```

### Status strip

No change: `REPLAY` badge exists. The data-age counter keeps measuring
snapshot arrival, which is correct in replay too.

## Error handling summary

| Situation | Behaviour |
|---|---|
| Recordings dir missing | Empty session list |
| Corrupt line | Skipped, counted in `skipped_lines` |
| `ingest` raises | Counted in `ingest_errors`, replay continues |
| Load while playing | Old player stopped and joined first |
| Seek past end / before start | Clamped |
| Speed outside range | 422 |
| Live or demo mode | 409 on every POST |
| Backend restart | Replay state is not persisted; UI returns to the picker |
| Outline file missing | Fallback trace |
| Snapshot without `x`/`y` | Driver not drawn, no error |

## Testing

Backend (`tests/test_replay.py`, `tests/test_replay_api.py`,
`tests/test_generate_outlines.py`):

- Recordings for tests are written with `RawMessage.to_line()` so the format
  cannot drift from the recorder.
- Reader: merge order across three topic files; malformed lines counted;
  `seek` skips without yielding.
- Player with a fake clock: pacing equals gap/speed; gap clamp; pause and
  resume; forward seek applies without sleeping; backward seek yields a
  fresh adapter whose state matches a from-scratch run to the same point;
  `finished` then `play()` restarts; ingest error counted; snapshot mode and
  `replay` block; hostile session keys (`../x`, `9999/../..`, symlink out of
  root) rejected without revealing a path.
- Catalog: lists digit directories only, computes start/end from first and
  last lines, missing root is empty.
- API: 409 in live and demo; token required on POST when configured, not on
  GET; `/health` and `/replay` never contain the recordings path; load →
  snapshot mode `replay`; unload → idle; unknown session 404.
- Outline generator with `MockTransport`: picks the fastest clean lap,
  windows the location query correctly, simplifies to ≤ 400 points, skips a
  circuit with no data, honours `--circuit`.
- Existing: generated types and fixture staleness tests still pass after
  `make types`.

Frontend (`TrackMap.test.tsx`, `ReplayBar.test.tsx`, `replayApi.test.ts`,
`useTrackTrace.test.ts`):

- TrackMap draws the bundled outline path for a known circuit; draws the
  trace when the circuit is unknown; one `data-driver` group per driver with
  coordinates; pit dimming; no group for a driver without x/y.
- ReplayBar: hidden in live/demo; picker in idle with mocked fetch; controls
  post to the right endpoints with the token header; slider change calls
  seek with a fraction.
- Fixture-driven leaderboard test unchanged apart from regeneration.

## Documentation

- README: write the "Replaying a session" section (how to load, controls,
  what `received_at` pacing means, the 409 rule), a "Track map" subsection
  under Architecture (outline source, `make outlines`, fallback), status
  table rows for Phases 3 and 4 marked Done, `PHASE = 4`.
- Makefile: `outlines` target and help line.
