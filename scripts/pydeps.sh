# Sourced by run_controller.sh and probe_sheets.sh: make the controller's Python dependencies
# importable, installing missing ones (pinned) into the repo's shared .pydeps/py<ver>/ target.
#   tm_pydeps <python> <project.yaml> <backend>
# Needs REPO set. Exports PYTHONPATH (deps, then the repo).
tm_pydeps() {
    local PY=$1 CONFIG=$2 BACKEND=${3:-canfar} DEPS NEED=()
    DEPS="$REPO/.pydeps/py$("$PY" -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]}")')"
    [[ -d $DEPS ]] && export PYTHONPATH="$DEPS:${PYTHONPATH:-}"
    "$PY" -c "import yaml" 2>/dev/null || NEED+=("pyyaml")
    if [[ $BACKEND == canfar ]] && ! "$PY" -c "import canfar" 2>/dev/null; then NEED+=("canfar==1.3.5"); fi
    _tm_install "$PY" "$DEPS" "${NEED[@]}"
    NEED=()
    if [[ $(PYTHONPATH="$REPO:${PYTHONPATH:-}" "$PY" -m trackmanager.config "$CONFIG" --get sheets.enabled 2>/dev/null) == true ]]; then
        "$PY" -c "import google.auth.transport.requests, google.oauth2.service_account" 2>/dev/null \
            || NEED+=("google-auth==2.63.1" "requests")
    fi
    _tm_install "$PY" "$DEPS" "${NEED[@]}"
    export PYTHONPATH="$REPO:${PYTHONPATH:-}"
}

_tm_install() {
    local PY=$1 DEPS=$2; shift 2
    (( $# )) || return 0
    echo "installing $* into $DEPS"
    "$PY" -m pip install --quiet --target "$DEPS" "$@"
    case ":${PYTHONPATH:-}:" in *":$DEPS:"*) ;; *) export PYTHONPATH="$DEPS:${PYTHONPATH:-}" ;; esac
}
