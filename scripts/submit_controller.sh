#!/usr/bin/env bash
# Launch the controller as a headless CANFAR session.
#   bash submit_controller.sh <config.json> <run root>
# The run root (the track database) lives in the project space, never inside this repo.
set -euo pipefail
REPO=$(dirname "$(dirname "$(readlink -f "$0")")")
CONFIG=$(readlink -f "${1:?usage: submit_controller.sh <config.json> <run root>}")
ROOT=$(readlink -m "${2:?usage: submit_controller.sh <config.json> <run root>}")
IMAGE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['controller']['image'])" "$CONFIG")
mkdir -p "$ROOT/controller"
NAME="tm-ctrl-$(basename "$ROOT" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | cut -c1-12)"
canfar create headless "$IMAGE" --name "$NAME" -- bash "$REPO/scripts/run_controller.sh" "$CONFIG" "$ROOT" \
    | tee -a "$ROOT/controller/submitted_sessions.txt"
