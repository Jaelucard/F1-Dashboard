# F1 Timing Dashboard

A real-time Formula 1 timing dashboard for the 2026 season, built on the
[OpenF1 API](https://openf1.org). Live leaderboard during a session, replay of a
recorded or historical session afterwards.

**Unofficial project. Not affiliated with, endorsed by, or connected to Formula 1,
Formula One Management, or the FIA.** Data is provided by the OpenF1 API.

---

## Status

| Phase | What it covers | State |
|---|---|---|
| 0 | Skeleton: FastAPI health route, WebSocket, React shell, `make dev` | Done |
| 1 | `auth.py` + `recorder.py`: token, MQTT, raw `.jsonl` capture | Done |
| 2 | Live state, `ws.py` snapshot, leaderboard, status strip | Done |
| 3 | `replay.py`: replay a recording with a session picker and transport bar | Done |
| 4 | Track map: bundled circuit outlines from OpenF1 history, live car dots | Done |
| 5-6 | 2026 aero mapping, historical REST adapter, radio, polish (Tier B) | Not started |

## Requirements

- **Python 3.13.** The backend is pinned to 3.13 (`PYTHON` in the Makefile).
  Python 3.14 is not yet supported: current pip releases cannot bootstrap
  inside a 3.14 venv on some platforms, so leave the venv's pip at the version
  `python -m venv` installs rather than upgrading it.
- **Node.js 20 or newer.**

## Setup

```bash
make setup          # venv + backend deps + frontend deps, creates backend/.env
```

Then fill in `backend/.env`:

```
OPENF1_USERNAME=your-openf1-email
OPENF1_PASSWORD=your-openf1-password
LIVE_MODE=false
```

`backend/.env` is gitignored and must never be committed. Only
`backend/.env.example` is tracked. The credentials are read exclusively through
environment variables in `auth.py`, are held as pydantic `SecretStr` so they
cannot be printed by accident, and are never sent to the browser - the browser
talks only to this backend, never to OpenF1.

## Running

```bash
make dev            # backend on :8000, frontend on :5173
```

Open **http://localhost:5173**. The Vite dev server proxies `/health` and `/ws`
to the backend, so everything is same-origin and no backend URL is baked into
the frontend.

Useful endpoints:

- `GET http://127.0.0.1:8000/health` - liveness plus `status`, `ready`, `problems`, feed state, recorder health
- `GET http://127.0.0.1:8000/ready` - readiness: 200 when the backend can do what it was configured for, 503 otherwise
- `GET http://127.0.0.1:8000/docs` - generated OpenAPI docs
- `ws://127.0.0.1:8000/ws` - the single snapshot stream the UI consumes

```bash
make demo           # synthetic 22-car grid, connects to nothing
make test           # backend (335) + frontend (160) test suites
make types          # regenerate the TypeScript types and test fixture
make lint           # ruff (backend), oxlint + tsc -b (frontend)
```

### Seeing it work outside a session window

When there is no livestream, `make demo` serves a synthetic 22-car
grid through the ordinary WebSocket path. It never connects to OpenF1. The
status strip shows `DEMO` as a method to see if the UI works, or if something
is broken and requires attention.

## Architecture

```
MQTT ──> recorder.py ──> recordings/<session_key>/<topic>.jsonl   (disk first)
                └────> openf1_live.py ──> SessionState ──> ws.py ──> browser
```

The recorder writes every message to disk *before* handing it to the live
adapter. The hand-off is a bounded queue drained by its own thread, so the
adapter never runs on paho's network thread: a slow adapter can lose *queue*
messages (counted as `fanout_dropped` in `/health`) but can neither stall the
MQTT loop nor cost the recording a message. The adapter's exceptions are caught
on the recorder's side, so a bug in the merge logic can break the leaderboard
but can never cost a recording. The adapter subscribes in-process rather than
opening a second MQTT connection.

`ws.py` runs one broadcaster: one snapshot is built and serialised per tick and
handed to every browser. Each browser has a one-slot mailbox (latest wins), a
send that takes longer than `WS_SEND_TIMEOUT_SECONDS` drops that browser, and
`WS_MAX_CLIENTS` caps how many can connect. If building a snapshot fails, the
last good one is re-sent with `degraded: true` and the reason, rather than a
blank idle frame that would look like the session had ended.

### One shared type

`backend/app/models.py` defines `SessionState`. `make types` generates
`frontend/src/types/sessionState.ts` from it, and a backend test fails if that
file is stale - so the two sides of the WebSocket cannot disagree about the
shape of a snapshot.

The same mechanism generates `frontend/src/test/sampleSnapshot.json` by feeding
sample messages through the real `OpenF1LiveSource`. The leaderboard test
renders exactly the bytes the backend would send, rather than a hand-written
guess about them.

### Why whole snapshots instead of diffs

`ws.py` pushes a complete `SessionState` once a second rather than streaming
per-message deltas. At a few hundred messages a second a diff stream would mean
a React render per message, and a browser that falls behind could never catch
up. A whole snapshot is small (~13 KB for a full grid), always self-consistent,
and means a browser reconnecting mid-session is correct immediately with no
replay of missed deltas. The steady beat is also what makes the data-age counter
meaningful: if snapshots stop, the backend is gone.

### Merging by `_key`

MQTT messages carry two fields the REST payloads do not: `_id`, an
ever-increasing ordinal, and `_key`, the document identity. The same `_key` on
the same topic is an **update to the same record**, which happens routinely on
`v1/laps` as sector times fill in. The adapter upserts by `_key` and merges
rather than replacing, so a revision carrying only sector 2 does not blank
sector 1. Where two messages share a `_key`, the higher `_id` wins, so an
out-of-order delivery cannot overwrite fresh data with stale data. Payloads
without a `_key` (all the REST ones) fall back to a natural key per topic. A
record that lacks the fields its natural key is built from (a lap with no
driver number, say) is **quarantined and counted**, never merged into a shared
`None:None` bucket.

The exception is telemetry. `position`, `intervals`, `location` and `car_data`
carry a fresh `_key` on every message, so keying them by `_key` would retain the
whole race in memory. They are keyed by driver instead: the latest sample per
driver drives the leaderboard, `location` and `car_data` keep a short ring
buffer per driver for the track map, and the full history lives in the
recording on disk. Every other store has a hard cap as well, so memory and
snapshot time stay flat however long the session runs.

### Scoped by session

Every payload carries `session_key`, and the adapter keeps a separate bucket
per session. Only the **active** session is rendered: the highest `session_key`
that has produced *timing* data. A `v1/sessions` or `v1/drivers` announcement
for a later session is stored but does not switch the board, because OpenF1 can
publish the weekend's schedule before a session starts. A late message from an
older session goes to that session's bucket (or is dropped once the bucket has
been evicted) and is counted as `late_session_messages`; it can never appear on
the new session's board. The last two sessions are retained.

### Validated before merging

Each topic has a small schema. Wrong-typed fields are dropped from the record
and counted (`malformed_fields`); records missing their identity are rejected
and counted (`quarantined_records`); telemetry samples that carry no data
after stripping are rejected rather than blanking a good reading. Race control
is ordered by parsed UTC time with `_id` as the tie-breaker, and a record with
an unparseable date can never become "the latest". All of this is reported in
the `adapter` block of every snapshot and under `source` in `/health`.

### Track map

The panel on the right draws every car from the latest `v1/location` sample
per driver (`x`, `y` on `DriverState`), in team colour, dimmed while in the
pits, gliding between the once-a-second snapshots.

The circuit outline underneath is **bundled**: `make outlines` runs
`backend/scripts/generate_outlines.py`, which takes each circuit's latest
completed session from the free OpenF1 REST API, finds the fastest clean lap,
fetches that lap's `location` samples and simplifies them to at most 400
points in `frontend/src/data/circuits/<circuit_key>.json`. Because the
outline comes from the same coordinate frame as the live stream, cars land on
the line with no per-circuit calibration. A circuit with no file (Jeddah has
no 2026 data on OpenF1; Madrid has not run yet) falls back to tracing where
the cars have been, so the map still draws itself as the session goes on.

## What the supporter account unlocks

For Free Users, the historical data from 2023 onwards is free and needs no authentication. 
A session becomes historical 30 minutes after it ends.

If you have the paid version, you can input your API key from OpenF1 and
utilise the live version of this product to your liking.

Authentication is OAuth2: `POST https://api.openf1.org/token` with a form body
of `username` and `password` returns an `access_token` valid for one hour. Live
data arrives over MQTT at `mqtt.openf1.org:8883` (TLS), using the access token
as the MQTT password. Topics mirror the REST paths (`v1/laps`, `v1/location`,
and so on).

## Recording a live session

Two ways to run, and you should run **one or the other, never both** - each
opens its own MQTT connection, and two connections would write every message
twice.

```bash
make record         # recorder only, no UI. The Tier A fallback.
make dev            # API + UI, with the recorder running inside the backend
```

`make record` is the one to trust when all that matters is capturing the
session. It imports only `config` and `auth`, so no bug in the adapters, the
WebSocket layer, or the frontend can stop it. `make dev` runs the same recorder
inside the FastAPI process; from Phase 2 the live adapter subscribes to it
in-process rather than opening a second connection, as the OpenF1 docs ask.

Set `LIVE_MODE=true` in `backend/.env` first. With it false, **neither** path
connects: `make dev` starts idle, and `make record` exits with code 2 before
requesting a token. To record deliberately with the flag off, pass
`--force-live` (`.venv/bin/python -m app.recorder --force-live`) or set
`RECORDER_FORCE_LIVE=1`.

Output lands in `recordings/<session_key>/<topic>.jsonl`, one JSON object per
line:

```json
{"received_at":"2026-09-04T11:30:15.123+00:00","topic":"v1/laps","payload":{...}}
```

`payload` is the message exactly as received, including OpenF1's `_id` and
`_key` fields. A message that is not valid JSON is still recorded, wrapped as
`{"_raw": "...", "_decode_error": "..."}` - the recording is the artefact, so
nothing is ever dropped for being unparseable. Every line is flushed to the OS
immediately, so you can `tail -f` a file while a session is running.

### Checking it is alive

```bash
curl -s localhost:8000/health | python3 -m json.tool     # with make dev
curl -si localhost:8000/ready | head -1                   # 200 or 503
```

`/health` always answers 200 - that is the liveness signal - but it does not
always say `ok`. `status` is `ok`, `degraded` or `error`, `ready` says whether
the backend can currently do what it was configured for, and `problems` lists
what is wrong in plain words. `/ready` turns the same evaluation into a status
code. Live mode with missing credentials, a rejected token, or failing writes
is an `error` and not ready; a stale feed, low disk, dropped fan-out messages
or a possibly incomplete recording is `degraded`.

The `recorder` block reports `connected`, `auth_state`, `messages_recorded`,
per-topic counts, `last_message_at`, `session_keys`, `token_expires_at`,
`recording_ok` / `write_failures` / `recording_error`, `disk_free_bytes` /
`disk_low`, the fan-out queue, and `id_tracking`. Outside a session window a
healthy recorder shows `connected: true` with `messages_recorded: 0` and logs
`connected, subscribed to '#' at QoS 1 - waiting for messages`. That is not a
fault; the broker is simply quiet.

The `feed` block (also on every snapshot) separates the things a LIVE badge
used to conflate: `mqtt_connected`, `authenticated`, `last_message_at`,
`data_age_seconds` (from the recorder, not the WebSocket push), and a single
`state`: `offline`, `connecting`, `auth_failed`, `connected` (up, nothing yet),
`live`, or `stale` (no message for `FEED_STALE_SECONDS`, default 15). The UI
shows LIVE only for `live` in live mode with the socket open; a synthetic grid
shows DEMO, and everything else is named for what is actually wrong.

### Is the recording complete?

The recorder never claims more than it knows. `may_be_incomplete` becomes true
- and stays true - after any write failure, any disconnect after data had
started flowing, or a gap in OpenF1's `_id` sequence. `id_tracking` reports
per-topic and global `min_id` / `max_id` / `count` / `duplicates` /
`out_of_order` / `missing`. OpenF1 documents `_id` as increasing but not
whether the sequence is per topic or shared; both models are computed and the
one whose arithmetic is consistent is used for `messages_possibly_lost`. Treat
it as a heuristic until the Monza FP1 recording confirms which it is.

Writes are classified: `disk_full` (ENOSPC), `permission` (EACCES/EPERM, a
read-only volume, or a failed `mkdir`) or `io`, at the `mkdir`, `open`, `write`
or `flush` stage. The first failure of a burst is logged at ERROR (then every
thousandth, so a full disk cannot also fill the log), the message still fans
out to the dashboard, the file handle is discarded so the next write retries
from scratch, and recovery is logged. Recording continues on a low disk: the
capture is the artefact, and stopping early loses more than it saves.

### Disk space

`location` and `car_data` both arrive at roughly 3.7 Hz per car. With 20 cars
that is around 150 messages a second, or very roughly **100 MB per hour of
running**. A full weekend is likely to be somewhere under a gigabyte. Check you
have the room before FP1.

Check what a session actually used:

```bash
du -sh recordings/*
```

If a machine is short on space and only needs the live dashboard, set
`RECORDING_ENABLED=false` in `backend/.env`. The recorder still connects and
feeds the live adapter - the dashboard keeps working - it just never writes to
disk; `/health` continues to report `recording_ok: true` and a running
`messages_recorded` count, just with nothing on disk to show for it. Leave it
on by default: replay, and the Phase 5 analysis of what `aero_raw` means in
2026, both depend on a recording existing.

To free space from old sessions:

```bash
make clean-recordings
```

Lists each `recordings/<session_key>` folder with `du -sh` and asks `y/N`
before deleting it, one folder at a time - it never deletes anything without
that confirmation.

### Token rotation

The OAuth2 token *is* the MQTT password and lasts one hour. Rather than waiting
to be disconnected by the broker at an unpredictable moment, the recorder
reconnects deliberately five minutes before expiry, while the current token is
still valid. Unexpected drops are handled by paho's automatic reconnect. A
refused connection (`not authorized`) *signals* the supervisor thread, which
tears the connection down and fetches a fresh token; the paho callback itself
never does HTTP, because a blocking retry loop on the network thread would
stall it. Token failures are classified (`credentials`, `network`, `server`,
`malformed`) and reported as `auth_state` / `last_error_kind` without ever
including a token or password. Files are appended to throughout, so a reconnect
is invisible in the recording - but it is counted, and the recording is marked
possibly incomplete if data had already been flowing.

The subscription uses QoS 1 (`MQTT_QOS`), so the broker retransmits anything
unacknowledged across a brief drop; the adapter is idempotent (same `_key`,
same `_id`) so a redelivery is harmless, and the recording keeps both copies
for replay to reconcile by `_id`.

### WebSocket access control

`/ws` checks the `Origin` header before completing the handshake (a browser
always sends one) against `WS_ALLOWED_ORIGINS`; anything else is refused with a
403. Requests with no `Origin` (curl, websocat) are allowed unless
`WS_REQUIRE_ORIGIN=true`. Setting `WS_AUTH_TOKEN` requires `?token=<value>` or
`Authorization: Bearer <value>`; the browser reads the value from
`VITE_WS_TOKEN` at build time or from `localStorage['f1dash.wsToken']`.
`WS_MAX_CLIENTS` (default 16) caps connections. Refusals are counted under
`websocket.rejected` in `/health`, and the token never appears there.

## Replaying a session

With the backend idle (`LIVE_MODE=false`, `DEMO_MODE=false`) the bar along
the bottom of the page lists every recording under `recordings/` and loads the
one you pick. Once loaded: play/pause, a speed selector (1x to 25x), a scrub
slider, and Unload. The status strip shows `REPLAY`.

How it works (`backend/app/replay.py`): the per-topic `.jsonl` files are
streamed in `received_at` order (a k-way merge, so memory stays flat however
long the session was) into a **fresh `OpenF1LiveSource`**. A replayed snapshot
therefore goes through exactly the validation, merging and session scoping a
live one does, and the browser cannot tell them apart except for the mode.
Pacing follows the recorded gaps divided by the speed, with any single gap
clamped to five seconds so a red flag does not freeze the replay. Seeking
forward applies messages without sleeping; seeking backward starts a fresh
adapter and fast-forwards, which takes a moment on a long session and shows
as `seeking`.

The same controls are available over HTTP, which is what the bar calls:

```
GET  /replay/sessions            what is on disk (no paths, only keys and times)
GET  /replay                     current state, or {"state": "idle"}
POST /replay/load                {"session_key": 11353}
POST /replay/play | /pause | /unload
POST /replay/seek                {"fraction": 0.5} or {"position": "<ISO time>"}
POST /replay/speed               {"speed": 5}
```

Replay is refused with `409` while a recorder is running or in demo mode, so
it can never compete with a live capture. When `WS_AUTH_TOKEN` is set, the
POST endpoints need it too (`Authorization: Bearer` or `?token=`). Replay
state is reported under `replay` in `/health` and on every snapshot.

## Notes on the OpenF1 API

Confirmed by hand against the live API, because the docs do not spell these out:

- **`/v1/car_data` must be windowed.** Asking for a whole session returns
  `{"detail": "No results found."}` rather than an error - it appears to be a
  result-size guard. Narrow it with `date>` / `date<` (a two-minute window for
  one driver returns ~445 rows, confirming ~3.7 Hz). The Phase 5 historical
  adapter has to page through time windows rather than ask for a session.
- **`gmt_offset` is a string**, formatted `"02:00:00"`, not a number of hours.
  `SessionPicker` has to parse it before doing local-time arithmetic.
- **`/v1/laps` has `date_start`**, not `date`, and also carries
  `segments_sector_1/2/3` which the build plan does not mention.
- **`/v1/pit` has three duration fields**: `pit_duration`, `lane_duration` and
  `stop_duration`.
- `team_colour` comes back as a bare hex string with no `#` (e.g. `"4781D7"`).

## 2026 regulations

This dashboard targets the 2026 rule set, in which **DRS no longer exists**. The
UI uses the current terms only: **Active Aero** (Straight Mode / Corner Mode),
**Overtake Mode**, **Boost**, and **Recharge**. There are no DRS indicators,
zones, or detection points anywhere in this project.

I'm still trying to optimise this by checking if i can track energy deployment throughout
the race, but after much research i think its unlikely due to the fact that 
the teams do consider battery usage/overtake mode readings too important to be made public :(

## Layout

```
backend/
  app/
    config.py        settings + credential handling
    main.py          FastAPI app, /health, /ready, /ws
    feed.py          feed state + health evaluation (pure functions)
    ws.py            WebSocket broadcaster, access control, slow-client drop
    adapters/        live data source (replay re-feeds the same adapter)
    replay.py        recording reader, catalog, and the replay player
    replay_api.py    /replay control endpoints
  scripts/
    generate_outlines.py   `make outlines`: circuit outlines from OpenF1 history
  tests/
frontend/
  src/
    lib/             socketController (lifecycle), validateSnapshot (runtime
                     checks), status (badge logic), format, shared types
    components/      Leaderboard, StatusStrip, TrackMap, ReplayBar
    data/circuits/   bundled circuit outlines (generated, committed)
recordings/          raw .jsonl captures, gitignored
```

## Attribution

Timing data from the [OpenF1 API](https://openf1.org).
