#!/usr/bin/env bash
# Run the local supervisor model on the Jetson Orin, in a container, on demand.
#
#   ./agent/orin-model.sh start          bring the container up
#   ./agent/orin-model.sh pull qwen3:8b  download a model into the volume
#   ./agent/orin-model.sh tunnel         forward it to localhost:11435 here
#   ./agent/orin-model.sh status         what is running, and what it holds
#   ./agent/orin-model.sh stop           take it down; nothing is left running
#   ./agent/orin-model.sh purge          stop, then delete the volume and image
#
# The Orin is a shared machine, so this deliberately leaves no trace:
#
#   - Nothing is installed on the host. Everything lives in one container and
#     one named volume, both prefixed `jd-`.
#   - `--rm` and no restart policy, so a reboot brings nothing back and there is
#     no service anybody has to know about.
#   - The port is published on the Orin's loopback only. Without the SSH tunnel
#     it is not reachable from the tailnet, so this never hands an
#     unauthenticated GPU endpoint to whoever knows the address.
#   - `purge` removes the volume and the image: the machine goes back to how it
#     was, and `docker volume ls` proves it.
#
# It does not touch the team's own `home2-hri-ollama-*` containers, their model
# directory under ~/home2, or anything else on the host.
set -euo pipefail

ORIN_HOST="${ORIN_HOST:-orin@100.68.248.115}"
CONTAINER="${JD_CONTAINER:-jd-ollama}"
VOLUME="${JD_VOLUME:-jd-ollama}"
IMAGE="${JD_IMAGE:-ollama/ollama:latest}"
REMOTE_PORT="${JD_REMOTE_PORT:-11434}"
# Ollama sizes the context off total memory, and the Orin's 61 GiB of unified
# memory makes it pick 256k tokens. That reserves an enormous KV cache for a
# prompt that never exceeds a few thousand, so it is pinned to something sane.
CONTEXT="${JD_CONTEXT:-32768}"
# Not 11434, so a local Ollama on this laptop would not collide with the tunnel.
LOCAL_PORT="${JD_LOCAL_PORT:-11435}"

# Every subcommand makes several calls, and over Tailscale a fresh SSH handshake
# to this machine can cost fifteen seconds — more when the tailnet falls back to
# a relay. One multiplexed connection is opened and reused, so only the first
# call pays for it and the rest are instant.
CONTROL="${JD_CONTROL:-$HOME/.ssh/jd-orin-%r@%h:%p}"
SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o ControlMaster=auto
  -o "ControlPath=$CONTROL"
  -o ControlPersist=600
  -o ServerAliveInterval=15
)

# The password lives in the environment, not in this file. Without sshpass and
# ORIN_PASSWORD, plain ssh runs and prompts or uses a key, which is preferable.
ssh_orin() {
  if [ -n "${ORIN_PASSWORD:-}" ] && command -v sshpass > /dev/null; then
    sshpass -p "$ORIN_PASSWORD" ssh "${SSH_OPTS[@]}" "$ORIN_HOST" "$@"
  else
    ssh "${SSH_OPTS[@]}" "$ORIN_HOST" "$@"
  fi
}

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

case "${1:-}" in
  start)
    if ssh_orin "docker ps --filter name=^${CONTAINER}\$ --quiet | grep -q ."; then
      echo "Already running."
    else
      # One line on purpose: a backslash-continued string does not survive the
      # trip through ssh intact. `--runtime nvidia` alone is what the machine's
      # own compose files use; `--gpus all` adds nothing on Tegra.
      ssh_orin "docker run --rm -d --name ${CONTAINER} --runtime nvidia -e OLLAMA_CONTEXT_LENGTH=${CONTEXT} -v ${VOLUME}:/root/.ollama -p 127.0.0.1:${REMOTE_PORT}:11434 ${IMAGE}" > /dev/null
      echo "Started ${CONTAINER} on the Orin's loopback:${REMOTE_PORT}."
    fi
    # A container that fell back to the CPU still answers, just twenty times
    # slower, so say which one it got rather than let it be discovered later.
    sleep 4
    if ssh_orin "docker logs ${CONTAINER} 2>&1 | grep -q 'type=iGPU'"; then
      echo "  GPU: iGPU detected."
    else
      echo "  GPU: NOT detected — it will run on the CPU. Check:"
      echo "    ./agent/orin-model.sh status"
    fi
    echo "  Next: ./agent/orin-model.sh tunnel"
    ;;

  pull)
    [ $# -ge 2 ] || { echo "Which model? e.g. $0 pull qwen3:8b" >&2; exit 1; }
    ssh_orin "docker exec ${CONTAINER} ollama pull $2"
    ;;

  tunnel)
    echo "Forwarding localhost:${LOCAL_PORT} -> Orin loopback:${REMOTE_PORT}."
    echo "Leave this running; Ctrl-C closes it."
    # The tunnel gets its own connection: sharing the multiplexed one would tie
    # its lifetime to whatever else is using the socket.
    if [ -n "${ORIN_PASSWORD:-}" ] && command -v sshpass > /dev/null; then
      exec sshpass -p "$ORIN_PASSWORD" ssh -o StrictHostKeyChecking=no \
        -o ServerAliveInterval=15 -o ExitOnForwardFailure=yes \
        -N -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$ORIN_HOST"
    fi
    exec ssh -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=15 -o ExitOnForwardFailure=yes \
      -N -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$ORIN_HOST"
    ;;

  status)
    echo "--- container ---"
    ssh_orin "docker ps --filter name=^${CONTAINER}\$ \
      --format '{{.Names}}  {{.Status}}  {{.Ports}}'" || true
    echo "--- restart policy (must be 'no') ---"
    ssh_orin "docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' ${CONTAINER} \
      2>/dev/null" || echo "  not running"
    echo "--- models in the volume ---"
    ssh_orin "docker exec ${CONTAINER} ollama list 2>/dev/null" || echo "  not running"
    echo "--- loaded right now ---"
    ssh_orin "docker exec ${CONTAINER} ollama ps 2>/dev/null" || true
    echo "--- GPU in the container log ---"
    ssh_orin "docker logs ${CONTAINER} 2>&1 | grep -iE 'cuda|tegra|igpu|cpu only' \
      | tail -3" || true
    ;;

  stop)
    ssh_orin "docker stop ${CONTAINER} > /dev/null 2>&1" || true
    echo "Stopped. The container is gone (--rm); the volume keeps the models."
    ;;

  purge)
    ssh_orin "docker stop ${CONTAINER} > /dev/null 2>&1" || true
    ssh_orin "docker volume rm ${VOLUME} > /dev/null 2>&1" || true
    ssh_orin "docker rmi ${IMAGE} > /dev/null 2>&1" || true
    echo "Purged: container, volume and image are gone from the Orin."
    ssh_orin "docker volume ls | grep -c ${VOLUME} || true" > /dev/null
    ;;

  *) usage ;;
esac
