#!/bin/sh
# Run the backend and the frontend together, and make sure both are gone when
# this exits. Called by `make dev` (argument: dev) and `make demo` (demo).
#
# Why this is a script and not two lines in the Makefile
# -----------------------------------------------------
# The old recipe was `trap 'kill 0' EXIT INT TERM; backend & frontend & wait`.
# On Ctrl-C the terminal sends SIGINT to every process in the group, then the
# INT trap sent SIGTERM to all of them, then the EXIT trap sent SIGTERM again.
# uvicorn's --reload supervisor handles these with a signal handler that is not
# re-entrant (it calls threading.Event.set(), which takes a plain lock), so a
# second signal landing while the first is still being handled deadlocks it.
# Seen on 2026-09-05: an orphaned reloader with no worker, still holding port
# 8000, ignoring SIGTERM, for 15 minutes. The next `make dev` could not bind
# the backend, while the frontend came up fine and sat on CONNECTING forever,
# because the dead reloader's socket accepted connections that nobody served.
#
# So this script:
#   1. refuses to start while either port is taken, and names the pid to kill;
#   2. signals each child at most once, and not at all on Ctrl-C, because the
#      terminal has already delivered SIGINT to both of them;
#   3. stops the other child when one dies, instead of running half-alive;
#   4. after a grace period, SIGKILLs anything still alive or still listening
#      on either port, so a stuck reloader can never poison the next run.
#
# BACKEND_PORT, FRONTEND_PORT, BACKEND_CMD, FRONTEND_CMD and GRACE_SECONDS can
# be overridden for testing this script. The ports the app really uses are
# fixed in frontend/vite.config.ts, so do not override them for real runs.

set -u

MODE=${1:-dev}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BACKEND_PORT=${BACKEND_PORT:-8000}
FRONTEND_PORT=${FRONTEND_PORT:-5173}
GRACE_SECONDS=${GRACE_SECONDS:-10}

case $MODE in
  dev)
    : "${BACKEND_CMD:=.venv/bin/uvicorn app.main:app --reload --port $BACKEND_PORT}"
    ;;
  demo)
    : "${BACKEND_CMD:=env DEMO_MODE=true LIVE_MODE=false .venv/bin/uvicorn app.main:app --port $BACKEND_PORT}"
    ;;
  *)
    echo "usage: $0 dev|demo" >&2
    exit 2
    ;;
esac
: "${FRONTEND_CMD:=npm run dev}"

# Pids listening on TCP port $1, space separated, deduplicated.
listeners() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u | tr '\n' ' '
}

# True while $1 exists and is not a zombie waiting to be reaped.
alive() {
  state=$(ps -o stat= -p "$1" 2>/dev/null)
  [ -n "$state" ] && [ "${state#Z}" = "$state" ]
}

refuse_if_busy() {  # $1 port, $2 what should be there
  pids=$(listeners "$1")
  [ -z "$pids" ] && return 0
  echo "error: port $1 (the $2) is already in use:" >&2
  ps -o pid,ppid,etime,command -p "$(echo "$pids" | sed 's/ *$//; s/ /,/g')" >&2
  echo "" >&2
  echo "A previous run is still there. Stop it, then try again:" >&2
  echo "    kill $pids" >&2
  echo "If it is still listening a few seconds later it is a stuck uvicorn" >&2
  echo "reloader (see the comment at the top of scripts/dev.sh); use:" >&2
  echo "    kill -9 $pids" >&2
  exit 1
}

refuse_if_busy "$BACKEND_PORT" backend
refuse_if_busy "$FRONTEND_PORT" frontend

if [ "$MODE" = demo ]; then
  echo "frontend -> http://localhost:$FRONTEND_PORT   (synthetic data, nothing connects)"
else
  echo "backend  -> http://127.0.0.1:$BACKEND_PORT"
  echo "frontend -> http://localhost:$FRONTEND_PORT   <- open this one"
fi

# `exec` so that $! is the real process (uvicorn, npm), not a wrapper subshell
# that would die on SIGTERM and orphan the real one. Word splitting of the
# *_CMD variables is intended.
# shellcheck disable=SC2086
( cd "$ROOT/backend" && exec $BACKEND_CMD ) &
BACKEND_PID=$!
# shellcheck disable=SC2086
( cd "$ROOT/frontend" && exec $FRONTEND_CMD ) &
FRONTEND_PID=$!

REASON=
trap 'REASON=interrupted' INT
trap 'REASON=terminated' TERM

while [ -z "$REASON" ] && alive "$BACKEND_PID" && alive "$FRONTEND_PID"; do
  sleep 1
done

case $REASON in
  interrupted)
    # The terminal already sent SIGINT to every process in the foreground
    # group. Sending anything more is what deadlocked the reloader before.
    echo ""
    echo "dev.sh: interrupted, waiting for the backend and the frontend to stop" >&2
    ;;
  terminated)
    # Only this shell was signalled; pass it on, once per child.
    echo "dev.sh: terminated, stopping the backend and the frontend" >&2
    kill -TERM "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
    ;;
  *)
    if alive "$BACKEND_PID"; then
      REASON=frontend-exited
      echo "dev.sh: the frontend exited, stopping the backend" >&2
      kill -TERM "$BACKEND_PID" 2>/dev/null
    else
      REASON=backend-exited
      echo "dev.sh: the backend exited (see its output above), stopping the frontend" >&2
      kill -TERM "$FRONTEND_PID" 2>/dev/null
    fi
    ;;
esac

deadline=$(( $(date +%s) + GRACE_SECONDS ))
while { alive "$BACKEND_PID" || alive "$FRONTEND_PID"; } && [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 0.2
done

# Anything still alive, or still holding a port, gets no further chance: a
# stuck reloader ignores SIGTERM, and the ports must be free for the next run.
leftover=""
for pid in $BACKEND_PID $FRONTEND_PID; do
  alive "$pid" && leftover="$leftover $pid"
done
leftover="$leftover $(listeners "$BACKEND_PORT") $(listeners "$FRONTEND_PORT")"
# shellcheck disable=SC2086
leftover=$(echo $leftover | tr ' ' '\n' | sort -u | tr '\n' ' ' | sed 's/ *$//')
if [ -n "$leftover" ]; then
  echo "dev.sh: still running ${GRACE_SECONDS}s after shutdown began, force-killing pid(s): $leftover" >&2
  # shellcheck disable=SC2086
  kill -9 $leftover 2>/dev/null
  sleep 0.5
fi
wait 2>/dev/null

echo "dev.sh: stopped ($REASON); ports $BACKEND_PORT and $FRONTEND_PORT are free" >&2
case $REASON in
  interrupted) exit 130 ;;
  terminated)  exit 143 ;;
  *)           exit 1 ;;
esac
