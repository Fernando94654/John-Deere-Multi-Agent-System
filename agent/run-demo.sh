#!/usr/bin/env bash
# Bring up the whole demo: the simulation with its MCP tools + web dashboard,
# and the OpenClaw gateway that supervises it.
#
#   ./agent/run-demo.sh                       # foreground, Ctrl-C stops both
#   ./agent/run-demo.sh --rows 20 --cols 28   # anything server.py takes
#   ./agent/run-demo.sh daemon                # detached: logs to files, returns your shell
#   ./agent/run-demo.sh daemon --seed 42      # detached, with extra server.py args
#   ./agent/run-demo.sh status                # is the detached run alive?
#   ./agent/run-demo.sh stop                  # stop a detached run
#
# Web dashboard + JSON API on :$WEB_PORT (default 8080). The operator drives the
# campaign from there and is the ONLY thing that starts a run: Start on the page
# -> POST /api/commands/start. The simulation comes up idle and waits for it.
# Set WEB_PORT=0 to turn the dashboard off, WEB_TOKEN=... to guard the mutating
# routes, AUTOSTART=1 to have the simulation build a run on its own instead.
#
# Ports: WebSocket 8765, MCP 8766, web/HTTP :$WEB_PORT, gateway :$PORT.
#
# The hook token is read from ~/.openclaw/openclaw.json, so the simulation and
# the gateway cannot drift apart on it.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"

MODE=run
case "${1:-}" in
  daemon|--daemon|-d) MODE=daemon; shift ;;
  stop|--stop)        MODE=stop;   shift ;;
  status|--status)    MODE=status; shift ;;
esac

RUNDIR="${JD_RUNDIR:-/tmp/jd-demo}"
WEB_PORT="${WEB_PORT:-8080}"
GW_LOG="/tmp/openclaw-gateway-demo.log"
SIM_LOG="$RUNDIR/sim.log"
mkdir -p "$RUNDIR"

# Kill a process and, if it leads a process group (it does when started under
# `setsid` below), the whole group with it.
kill_tree() {
  local pid=$1
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.5; done
  kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

# PIDs holding a local TCP listening port. Used only to reap a leftover
# simulation of ours, not arbitrary processes.
pids_on_port() {
  ss -ltnp 2>/dev/null | grep -E ":$1\b" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

# Free a port a stale demo process is still holding, and wait for it to let go.
# Returns non-zero if it never does.
free_port() {
  local port=$1 label=$2 pid
  [ "$port" = "0" ] && return 0
  ss -ltn 2>/dev/null | grep -qE ":$port\b" || return 0
  echo "Port $port ($label) is busy; stopping whatever holds it..."
  for pid in $(pids_on_port "$port"); do kill_tree "$pid"; done
  for _ in $(seq 20); do
    ss -ltn 2>/dev/null | grep -qE ":$port\b" || return 0
    sleep 0.5
  done
  return 1
}

if [ "$MODE" = stop ]; then
  echo "Stopping the detached run..."
  for name in sim gateway; do
    f="$RUNDIR/$name.pid"
    if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
      kill_tree "$(cat "$f")"; echo "  $name stopped"
    else
      echo "  $name was not running"
    fi
    rm -f "$f"
  done
  exit 0
fi

if [ "$MODE" = status ]; then
  for name in gateway sim; do
    f="$RUNDIR/$name.pid"
    if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
      echo "  $name: running (pid $(cat "$f"))"
    else
      echo "  $name: not running"
    fi
  done
  echo "  logs: $GW_LOG , $SIM_LOG"
  exit 0
fi

CONFIG="${OPENCLAW_CONFIG_PATH:-$HOME/.openclaw/openclaw.json}"
PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

command -v openclaw >/dev/null || {
  echo "openclaw is not on PATH. See README §11.6." >&2; exit 1; }
[ -f "$CONFIG" ] || {
  echo "No config at $CONFIG. Copy agent/openclaw.example.json5 there." >&2; exit 1; }

# `openclaw config get` redacts secrets, so the token has to come from the file.
# Errors are shown rather than swallowed: the README tells you to copy a .json5
# example over this path, and strict JSON chokes on its comments — which used to
# surface as a puzzling "no hooks.token" instead of "your config did not parse".
TOKEN=$(python3 -c "
import json, sys
try:
    print(json.load(open('$CONFIG')).get('hooks', {}).get('token', ''))
except json.JSONDecodeError as error:
    sys.exit(f'{error}. If you copied the .json5 example, strip its comments — '
             'OpenClaw reads JSON5, this script reads JSON.')") || {
  echo "Could not read $CONFIG" >&2; exit 1; }
[ -n "$TOKEN" ] || { echo "No hooks.token in $CONFIG." >&2; exit 1; }

# This one goes through OpenClaw, which parses its own config properly.
PORT=$(openclaw config get gateway.port 2>/dev/null | grep -E '^[0-9]+$' || echo 18789)

# Detached mode puts each child in its own session so closing the terminal does
# not take them down; foreground mode keeps them as ordinary jobs and traps
# Ctrl-C. `set -u` is happy with an empty DETACH because it is expanded unquoted.
DETACH=
if [ "$MODE" = daemon ]; then
  DETACH="setsid"
else
  cleanup() {
    trap - INT TERM EXIT
    echo
    echo "Stopping..."
    [ -n "${SIM_PID:-}" ] && kill_tree "$SIM_PID" || true
    [ -n "${GW_PID:-}" ] && kill_tree "$GW_PID" || true
    rm -f "$RUNDIR/sim.pid" "$RUNDIR/gateway.pid"
    wait 2>/dev/null || true
  }
  trap cleanup INT TERM EXIT
fi

# A gateway that is still shutting down still owns the state directory, and a
# new one started too soon refuses to run and dies quietly — leaving the demo
# with a banner saying "ready" and nothing listening.
if ss -ltn 2>/dev/null | grep -q ":$PORT\b"; then
  echo "Port $PORT is busy; stopping whatever holds it..."
  openclaw gateway stop --force > /dev/null 2>&1 || true
  [ -f "$RUNDIR/gateway.pid" ] && kill_tree "$(cat "$RUNDIR/gateway.pid")" || true
  for _ in $(seq 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT\b" || break
    sleep 1
  done
  ss -ltn 2>/dev/null | grep -q ":$PORT\b" && {
    echo "Port $PORT never freed up." >&2; exit 1; }
fi

# A previous demo — or one killed by closing the terminal rather than Ctrl-C —
# can leave server.py holding its three ports. A fresh one then dies on bind
# with "address already in use" while the banner still says "ready". Reap it:
# first by our own pid file, then by whoever is on the ports.
if [ -f "$RUNDIR/sim.pid" ] && kill -0 "$(cat "$RUNDIR/sim.pid")" 2>/dev/null; then
  echo "A previous simulation is still running; stopping it..."
  kill_tree "$(cat "$RUNDIR/sim.pid")"
fi
rm -f "$RUNDIR/sim.pid"
free_port 8765 WebSocket    || { echo "WebSocket port 8765 never freed up." >&2; exit 1; }
free_port 8766 MCP          || { echo "MCP port 8766 never freed up." >&2; exit 1; }
free_port "$WEB_PORT" web   || { echo "Web port $WEB_PORT never freed up." >&2; exit 1; }

echo "Starting the gateway..."
$DETACH openclaw gateway > "$GW_LOG" 2>&1 &
GW_PID=$!
echo "$GW_PID" > "$RUNDIR/gateway.pid"
for _ in $(seq 60); do
  grep -q "ready" "$GW_LOG" && break
  sleep 1
done
grep -q "ready" "$GW_LOG" || {
  echo "The gateway did not come up; see $GW_LOG" >&2
  tail -5 "$GW_LOG" >&2; exit 1; }
# "ready" in the log is not proof it survived: check the socket too.
kill -0 "$GW_PID" 2>/dev/null && ss -ltn 2>/dev/null | grep -q ":$PORT\b" || {
  echo "The gateway said ready and then exited; see the log." >&2
  tail -5 "$GW_LOG" >&2; exit 1; }
echo "  gateway ready on :$PORT"

# server.py args. Field defaults match the old demo; anything in "$@" is
# appended, so a flag you pass on the command line overrides the default.
SIM_ARGS=(--with-mcp)
# No --autostart by default: the run stays idle until the operator hits Start on
# the web dashboard (POST /api/commands/start). Set AUTOSTART=1 to override.
[ "${AUTOSTART:-0}" = "1" ] && SIM_ARGS+=(--autostart)
SIM_ARGS+=(
  --rows 16 --cols 22 --harvesters 4 --carts 2 --delay 1.0
  --host 127.0.0.1 --port 8765 --mcp-port 8766
  --wake-url "http://127.0.0.1:$PORT/hooks/agent" --wake-token "$TOKEN"
)
[ "$WEB_PORT" != "0" ] && SIM_ARGS+=(--web-port "$WEB_PORT")
[ -n "${WEB_TOKEN:-}" ] && SIM_ARGS+=(--web-token "$WEB_TOKEN")
SIM_ARGS+=("$@")

echo "Starting the simulation..."
if [ "$MODE" = daemon ]; then
  $DETACH "$PYTHON" -u Servidor/server.py "${SIM_ARGS[@]}" > "$SIM_LOG" 2>&1 &
else
  "$PYTHON" -u Servidor/server.py "${SIM_ARGS[@]}" &
fi
SIM_PID=$!
echo "$SIM_PID" > "$RUNDIR/sim.pid"

# server.py binds three ports (WebSocket, MCP, and — unless WEB_PORT=0 — the
# web/HTTP dashboard). Wait for all of them, and stop here with the reason if
# the process falls over instead of printing a "ready" banner for something
# that is not listening.
SIM_PORTS=(8765 8766)
SIM_PORT_NAMES=(WebSocket MCP)
if [ "$WEB_PORT" != "0" ]; then SIM_PORTS+=("$WEB_PORT"); SIM_PORT_NAMES+=(web); fi

sim_dead() {
  echo "The simulation exited on startup; nothing is serving the demo." >&2
  [ "$MODE" = daemon ] && { echo "--- last lines of $SIM_LOG ---" >&2; tail -20 "$SIM_LOG" >&2; }
  kill "$SIM_PID" 2>/dev/null || true
  exit 1
}

for _ in $(seq 30); do
  kill -0 "$SIM_PID" 2>/dev/null || sim_dead
  ready=1
  for p in "${SIM_PORTS[@]}"; do
    ss -ltn 2>/dev/null | grep -qE ":$p\b" || ready=0
  done
  [ "$ready" = 1 ] && break
  sleep 0.5
done

kill -0 "$SIM_PID" 2>/dev/null || sim_dead
for i in "${!SIM_PORTS[@]}"; do
  ss -ltn 2>/dev/null | grep -qE ":${SIM_PORTS[$i]}\b" || {
    echo "The simulation is up but nothing is listening on ${SIM_PORTS[$i]} (${SIM_PORT_NAMES[$i]})." >&2
    [ "$MODE" = daemon ] && { echo "--- last lines of $SIM_LOG ---" >&2; tail -20 "$SIM_LOG" >&2; }
    kill "$SIM_PID" 2>/dev/null || true
    exit 1
  }
done
if [ "$WEB_PORT" != "0" ]; then
  echo "  simulation ready — WebSocket :8765, MCP :8766, web :$WEB_PORT (idle; press Start on the web dashboard)"
else
  echo "  simulation ready — WebSocket :8765, MCP :8766 (idle; POST /api/commands/start once the web dashboard is on)"
fi

openclaw mcp probe johndeere || true

# `channels list` without --all shows only what is configured, so anything here
# is worth reporting. Finding out that the chat channel came down belongs in
# this banner, not in front of an audience.
if openclaw channels list 2>/dev/null | grep -qiE "telegram|discord|whatsapp|signal"; then
  echo
  openclaw channels status 2>&1 | grep -iE "telegram|discord|whatsapp|signal" \
    || echo "  chat channel: no status"
fi

if [ "$MODE" = daemon ]; then
  cat <<EOF

Detached. Both processes run in their own sessions and survive this shell.

  Gateway     :$PORT            (log: $GW_LOG)
  WebSocket   ws://127.0.0.1:8765
  MCP         http://127.0.0.1:8766/mcp
  Web + API   http://127.0.0.1:${WEB_PORT}/       (log: $SIM_LOG)

  The run is idle. The web API is the only thing that starts it:
    curl -sX POST http://127.0.0.1:${WEB_PORT}/api/commands/start

  Then drive it over the web API (bodies are JSON; see Servidor/WEB_API.md):
    curl -s http://127.0.0.1:${WEB_PORT}/api/state | head
    curl -sX POST http://127.0.0.1:${WEB_PORT}/api/rebalance
    curl -sX POST http://127.0.0.1:${WEB_PORT}/api/announce \\
      -H 'content-type: application/json' -d '{"text":"hola"}'

  ./agent/run-demo.sh status     # check
  ./agent/run-demo.sh stop       # stop both

EOF
  exit 0
fi

cat <<EOF

Ready. Ctrl-C stops both.

  WebSocket   ws://127.0.0.1:8765
  MCP         http://127.0.0.1:8766/mcp
  Web + API   http://127.0.0.1:${WEB_PORT}/
  Gateway     :$PORT

  The run is idle. Start it from the web dashboard — open the page and press
  Start, or:  curl -sX POST http://127.0.0.1:${WEB_PORT}/api/commands/start

  Talk to the supervisor:
    openclaw agent --agent farm-manager --session-key harvest -m "¿Cómo va la cosecha?"

  Once the run is going, the simulation wakes the supervisor on its own when the
  fleet gets stuck. With a chat channel linked, the same works from your phone.

EOF
wait "$SIM_PID"
