#!/usr/bin/env bash
# Bring up the whole demo: the simulation with WebSocket, MCP tools and the web
# dashboard, and the OpenClaw gateway that supervises it. Ctrl-C stops both.
#
#   ./agent/run-demo.sh                       # idle; defaults: 10x12, 2 harvesters, 2 carts
#   ./agent/run-demo.sh --rows 20 --cols 28   # anything server.py takes
#   ./agent/run-demo.sh --web-port 8081       # dashboard on a different port
#
# The hook token is read from ~/.openclaw/openclaw.json, so the simulation and
# the gateway cannot drift apart on it.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"

# The claude-cli backend spawns the operator's own `claude`, which would read
# ~/.claude/CLAUDE.md and carry their personal instructions into every turn.
# This config dir keeps the login and drops the rest.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.openclaw/claude-home}"

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

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "Stopping..."
  [ -n "${SIM_PID:-}" ] && kill "$SIM_PID" 2>/dev/null || true
  [ -n "${GW_PID:-}" ] && kill "$GW_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Respect server.py port overrides, including --port=8767.
SIM_PORT=8765
MCP_PORT=8766
WEB_PORT=8080
SERVER_HOST=127.0.0.1
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[i]}" in
    --port|--mcp-port|--web-port|--host)
      option="${args[i]}"
      ((i+=1))
      value="${args[i]:-}"
      [ -n "$value" ] || { echo "Missing value for $option" >&2; exit 1; }
      ;;
    --port=*|--mcp-port=*|--web-port=*|--host=*)
      option="${args[i]%%=*}"
      value="${args[i]#*=}"
      ;;
    *) continue ;;
  esac
  case "$option" in
    --port) SIM_PORT="$value" ;;
    --mcp-port) MCP_PORT="$value" ;;
    --web-port) WEB_PORT="$value" ;;
    --host) SERVER_HOST="$value" ;;
  esac
done

command -v lsof >/dev/null || {
  echo "Install lsof to release occupied demo ports." >&2; exit 1; }
for port in "$PORT" "$SIM_PORT" "$MCP_PORT" "$WEB_PORT"; do
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port <= 65535)) || {
    echo "Invalid port: $port" >&2; exit 1; }
done
# Port 0 disables the web API; a fixed Unity/MCP port is needed for readiness.
((SIM_PORT > 0 && MCP_PORT > 0 && PORT > 0)) || {
  echo "Gateway, Unity and MCP ports must be greater than zero." >&2; exit 1; }
declare -A seen_ports=()
for port in "$PORT" "$SIM_PORT" "$MCP_PORT" "$WEB_PORT"; do
  ((port == 0)) && continue
  [ -z "${seen_ports[$port]:-}" ] || {
    echo "Two demo services cannot share port $port." >&2; exit 1; }
  seen_ports[$port]=1
done

port_busy() { [ -n "$(ss -H -ltn "sport = :$1")" ]; }
release_port() {
  local port="$1" attempt
  local -a pids=()
  ((port == 0)) && return 0
  port_busy "$port" || return 0
  mapfile -t pids < <(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u)
  ((${#pids[@]})) || {
    echo "Port $port is busy but its owner is not accessible. Stop it manually." >&2
    return 1
  }
  echo "Releasing port $port (PID: ${pids[*]})..."
  kill -TERM "${pids[@]}" 2>/dev/null || true
  for ((attempt=0; attempt<20; attempt++)); do
    port_busy "$port" || return 0
    sleep 0.25
  done
  # Re-read owners: a previous demo's cleanup may already have stopped them.
  mapfile -t pids < <(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u)
  if ((${#pids[@]})); then
    echo "Port $port still busy; forcing its listener to stop..."
    kill -KILL "${pids[@]}" 2>/dev/null || true
  fi
  for ((attempt=0; attempt<20; attempt++)); do
    port_busy "$port" || return 0
    sleep 0.25
  done
  echo "Could not release port $port; startup cancelled." >&2
  return 1
}

# Stop the old simulation first so its wrapper can clean up the old gateway.
for port in "$SIM_PORT" "$MCP_PORT" "$WEB_PORT" "$PORT"; do
  release_port "$port"
done

echo "Starting the gateway..."
openclaw gateway > /tmp/openclaw-gateway-demo.log 2>&1 &
GW_PID=$!
for _ in $(seq 60); do
  kill -0 "$GW_PID" 2>/dev/null || break
  grep -q "ready" /tmp/openclaw-gateway-demo.log && break
  sleep 1
done
grep -q "ready" /tmp/openclaw-gateway-demo.log || {
  echo "The gateway did not come up; see /tmp/openclaw-gateway-demo.log" >&2
  tail -5 /tmp/openclaw-gateway-demo.log >&2; exit 1; }
# "ready" in the log is not proof it survived: check the socket too.
kill -0 "$GW_PID" 2>/dev/null && ss -ltn 2>/dev/null | grep -q ":$PORT\b" || {
  echo "The gateway said ready and then exited; see the log." >&2
  tail -5 /tmp/openclaw-gateway-demo.log >&2; exit 1; }
echo "  gateway ready on :$PORT"

echo "Starting the simulation..."
"$PYTHON" -u Servidor/server.py --with-mcp \
  --delay 1.0 \
  --host 127.0.0.1 --port 8765 --mcp-port 8766 --web-port 8080 \
  "$@" &
SIM_PID=$!
simulation_ready() {
  local port
  kill -0 "$SIM_PID" 2>/dev/null || return 1
  for port in "$SIM_PORT" "$MCP_PORT" "$WEB_PORT"; do
    ((port == 0)) && continue
    # Only the newly started server counts, not another process on that port.
    lsof -nP -a -p "$SIM_PID" -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | grep -q . || return 1
  done
}
for ((attempt=0; attempt<60; attempt++)); do
  kill -0 "$SIM_PID" 2>/dev/null || break
  simulation_ready && break
  sleep 0.5
done
simulation_ready || {
  echo "Simulation failed to start on its configured ports; startup cancelled." >&2
  exit 1
}

openclaw mcp probe johndeere || {
  echo "MCP initialization failed. Check that OpenClaw's johndeere URL uses port $MCP_PORT." >&2
  exit 1
}

# `channels list` without --all shows only what is configured, so anything here
# is worth reporting. Finding out that the chat channel came down belongs in
# this banner, not in front of an audience.
if openclaw channels list 2>/dev/null | grep -qiE "telegram|discord|whatsapp|signal"; then
  echo
  openclaw channels status 2>&1 | grep -iE "telegram|discord|whatsapp|signal" \
    || echo "  chat channel: no status"
fi

simulation_ready && kill -0 "$GW_PID" 2>/dev/null || {
  echo "A demo service exited during startup." >&2; exit 1; }
WEB_ADDRESS="disabled"
((WEB_PORT == 0)) || WEB_ADDRESS="http://$SERVER_HOST:$WEB_PORT"
cat <<EOF

Ready. Simulation idle until an explicit start/restart (unless --autostart was passed).
Service addresses:
  Web dashboard: $WEB_ADDRESS
  Unity WebSocket: ws://$SERVER_HOST:$SIM_PORT
  MCP tools: http://$SERVER_HOST:$MCP_PORT/mcp

  Talk to the supervisor:
    openclaw agent --agent farm-manager --session-key harvest -m "¿Cómo va la cosecha?"

  The simulation still spots trouble and records it — ask for list_recent_events —
  but it never calls the model on its own. Every turn is one you asked for.
  With a chat channel linked, the same works from your phone.

EOF
wait $SIM_PID
