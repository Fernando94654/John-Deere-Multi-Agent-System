#!/usr/bin/env bash
# Bring up the whole demo: the simulation with its MCP tools, and the OpenClaw
# gateway that supervises it. Ctrl-C stops both.
#
#   ./agent/run-demo.sh                       # 16x22, 4 harvesters, 2 carts
#   ./agent/run-demo.sh --rows 20 --cols 28   # anything server.py takes
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
trap cleanup INT TERM EXIT

# A gateway that is still shutting down still owns the state directory, and a
# new one started too soon refuses to run and dies quietly — leaving the demo
# with a banner saying "ready" and nothing listening.
if ss -ltn 2>/dev/null | grep -q ":$PORT\b"; then
  echo "Port $PORT is busy; stopping whatever holds it..."
  openclaw gateway stop --force > /dev/null 2>&1 || true
  for _ in $(seq 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT\b" || break
    sleep 1
  done
  ss -ltn 2>/dev/null | grep -q ":$PORT\b" && {
    echo "Port $PORT never freed up." >&2; exit 1; }
fi

echo "Starting the gateway..."
openclaw gateway > /tmp/openclaw-gateway-demo.log 2>&1 &
GW_PID=$!
for _ in $(seq 60); do
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
"$PYTHON" -u Servidor/server.py --with-mcp --autostart \
  --rows 16 --cols 22 --harvesters 4 --carts 2 --delay 1.0 \
  --host 127.0.0.1 --mcp-port 8766 \
  "$@" &
SIM_PID=$!
sleep 3

openclaw mcp probe johndeere || true

# `channels list` without --all shows only what is configured, so anything here
# is worth reporting. Finding out that the chat channel came down belongs in
# this banner, not in front of an audience.
if openclaw channels list 2>/dev/null | grep -qiE "telegram|discord|whatsapp|signal"; then
  echo
  openclaw channels status 2>&1 | grep -iE "telegram|discord|whatsapp|signal" \
    || echo "  chat channel: no status"
fi

cat <<EOF

Ready. Unity connects to ws://127.0.0.1:8765.

  Talk to the supervisor:
    openclaw agent --agent farm-manager --session-key harvest -m "¿Cómo va la cosecha?"

  The simulation still spots trouble and records it — ask for list_recent_events —
  but it never calls the model on its own. Every turn is one you asked for.
  With a chat channel linked, the same works from your phone.

EOF
wait $SIM_PID
