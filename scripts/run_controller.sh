#!/usr/bin/env bash
# Entry point for the controller inside a headless session (also used for successors).
#   bash run_controller.sh <project.yaml> <run root>
# Installs missing pinned deps (pyyaml, canfar, google-auth for sheets) into a shared target dir.
set -euo pipefail
REPO=$(dirname "$(dirname "$(readlink -f "$0")")")
CONFIG=$1; ROOT=$2
mkdir -p "$ROOT/controller/logs"
exec > >(tee -a "$ROOT/controller/logs/controller-${skaha_sessionid:-nosession}.log") 2>&1

sleep "${TM_START_DELAY_S:-0}"   # test hook: delay a successor's start
PY=${TM_PYTHON:-python3}
source "$REPO/scripts/pydeps.sh"
tm_pydeps "$PY" "$CONFIG" "${TM_BACKEND:-canfar}"   # pyyaml, canfar; google-auth when sheets are enabled
export TM_HEADLESS=1
echo "$(date -u +%FT%TZ) controller session ${skaha_sessionid:-?} on $(hostname); python $("$PY" -V 2>&1)"
exec "$PY" -m trackmanager.controller --project "$CONFIG" --root "$ROOT" --backend "${TM_BACKEND:-canfar}"
