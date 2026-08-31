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
| 2 | Live state, `ws.py` snapshot, leaderboard, status strip | Not started |
| 3-6 | Replay, track map, 2026 aero mapping, radio, polish (Tier B) | Not started |

The first live test is the Italian Grand Prix at Monza, FP1 on Friday 4 September 2026.

## Requirements

- **Python 3.13.** Not 3.14 on this machine: Homebrew's 3.14 ships a pip whose
  vendored `truststore` crashes on macOS 26 (`platform.mac_ver()` returns `''`),
  so `python3.14 -m venv` cannot bootstrap pip at all. For the same reason, do
  **not** run `pip install --upgrade pip` inside the venv - the suggested
  26.2.1 is the broken version. 25.1.1 works.
- **Node 20+** (developed on Node 26).

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

- `GET http://127.0.0.1:8000/health` - status, live mode, whether credentials loaded
- `GET http://127.0.0.1:8000/docs` - generated OpenAPI docs
- `ws://127.0.0.1:8000/ws` - the single snapshot stream the UI consumes

```bash
make test           # backend test suite
make lint           # frontend eslint + tsc --noEmit
```

## What the supporter account unlocks

Historical data from 2023 onwards is free and needs no authentication. A session
becomes historical 30 minutes after it ends.

The supporter account unlocks the **live window**: from 30 minutes before a
session starts to 30 minutes after it ends. That is the only way to get data
during a session, and it is what makes the recorder worth running on a Friday.
It also doubles the REST rate limit over the free tier's 3 requests/second and
30 requests/minute.

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

Set `LIVE_MODE=true` in `backend/.env` first, or the backend will not connect.

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
```

The `recorder` block reports `connected`, `messages_recorded`, per-topic counts,
`last_message_at`, `session_keys` and `token_expires_at`. Outside a session
window a healthy recorder shows `connected: true` with `messages_recorded: 0`
and logs `connected, subscribed to '#' - waiting for messages`. That is not a
fault; the broker is simply quiet.

### Disk space

`location` and `car_data` both arrive at roughly 3.7 Hz per car. With 20 cars
that is around 150 messages a second, or very roughly **100 MB per hour of
running**. A full weekend is likely to be somewhere under a gigabyte. Check you
have the room before FP1.

### Token rotation

The OAuth2 token *is* the MQTT password and lasts one hour. Rather than waiting
to be disconnected by the broker at an unpredictable moment, the recorder
reconnects deliberately five minutes before expiry, while the current token is
still valid. Unexpected drops are handled by paho's automatic reconnect, and a
refused connection forces a fresh token. Files are appended to throughout, so a
reconnect is invisible in the recording.

## Replaying a session

*(Phase 3 - not yet implemented.)*

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

OpenF1's `car_data` still exposes a legacy field named `drs`, and its docs have
not been updated for 2026. It is currently unknown whether that field carries
the aero state, Overtake Mode, or nothing at all. Until the Monza FP1 recording
is analysed, it is treated as an opaque integer: recorded raw, surfaced as
`aero_raw`, and never interpreted. The AERO and OT columns stay blank until the
evidence supports a mapping. If the field turns out to be dead, those columns
will be removed rather than left permanently empty.

## Layout

```
backend/
  app/
    config.py        settings + credential handling
    main.py          FastAPI app, /health, /ws
    ws.py            WebSocket fan-out to browsers
    adapters/        (Phase 2+) live / replay / historical data sources
  tests/
frontend/
  src/
    lib/             WebSocket hook, data-age counter, shared types
    components/      (Phase 2+) Leaderboard, StatusStrip, ...
recordings/          raw .jsonl captures, gitignored
```

## Attribution

Timing data from the [OpenF1 API](https://openf1.org).
