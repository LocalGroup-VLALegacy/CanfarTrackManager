#!/usr/bin/env bash
# Launch the controller as a headless CANFAR session.
#   bash submit_controller.sh <project.yaml> [run root]   (default: project.root)
# Use TM_PYTHON=/opt/conda/bin/python if python3 lacks pyyaml.
# The run root (the track database) lives in the project space, never inside this repo.
set -euo pipefail
REPO=$(dirname "$(dirname "$(readlink -f "$0")")")
CONFIG=$(readlink -f "${1:?usage: submit_controller.sh <project.yaml> [run root]}")
tmcfg() { PYTHONPATH="$REPO" "${TM_PYTHON:-python3}" -m trackmanager.config "$CONFIG" "$@"; }
ROOT=$(readlink -m "${2:-$(tmcfg --get project.root)}")
IMAGE=$(tmcfg --root "$ROOT" --get controller.image)
mkdir -p "$ROOT/controller"
NAME="tm-ctrl-$(basename "$ROOT" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | cut -c1-12)"
canfar create headless "$IMAGE" --name "$NAME" -- bash "$REPO/scripts/run_controller.sh" "$CONFIG" "$ROOT" \
    | tee -a "$ROOT/controller/submitted_sessions.txt"
