#!/usr/bin/env bash
# Entry point for the controller inside a headless session (also used for successors).
#   bash run_controller.sh <project.yaml> <run root>
# Installs the pinned canfar client into a shared target dir if the image lacks it.
set -euo pipefail
REPO=$(dirname "$(dirname "$(readlink -f "$0")")")
CONFIG=$1; ROOT=$2
mkdir -p "$ROOT/controller/logs"
exec > >(tee -a "$ROOT/controller/logs/controller-${skaha_sessionid:-nosession}.log") 2>&1

sleep "${TM_START_DELAY_S:-0}"   # test hook: delay a successor's start
PY=${TM_PYTHON:-python3}
DEPS="$REPO/.pydeps/py$("$PY" -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]}")')"
[[ -d $DEPS ]] && export PYTHONPATH="$DEPS:${PYTHONPATH:-}"
NEED=()
"$PY" -c "import yaml" 2>/dev/null || NEED+=("pyyaml")
if [[ ${TM_BACKEND:-canfar} == canfar ]] && ! "$PY" -c "import canfar" 2>/dev/null; then NEED+=("canfar==1.3.5"); fi
if (( ${#NEED[@]} )); then
    echo "installing ${NEED[*]} into $DEPS"
    "$PY" -m pip install --quiet --target "$DEPS" "${NEED[@]}"
    export PYTHONPATH="$DEPS:${PYTHONPATH:-}"
fi
export PYTHONPATH="$REPO:${PYTHONPATH:-}" TM_HEADLESS=1
echo "$(date -u +%FT%TZ) controller session ${skaha_sessionid:-?} on $(hostname); python $("$PY" -V 2>&1)"
exec "$PY" -m trackmanager.controller --project "$CONFIG" --root "$ROOT" --backend "${TM_BACKEND:-canfar}"
