#!/usr/bin/env bash
# Probe P2 (M4): can this session reach the Google Sheets API with the project's service account?
# Reads both spreadsheets' metadata (no writes) and records the result in <root>/probes/.
#   canfar create headless images.canfar.net/skaha/base-notebook:latest --name tm-sheets-probe -- \
#       bash <repo>/scripts/probe_sheets.sh <project.yaml>
set -euo pipefail
REPO=$(dirname "$(dirname "$(readlink -f "$0")")")
CONFIG=$(readlink -f "${1:?usage: probe_sheets.sh <project.yaml>}")
PY=${TM_PYTHON:-python3}
source "$REPO/scripts/pydeps.sh"
tm_pydeps "$PY" "$CONFIG" none
exec "$PY" - "$CONFIG" <<'PYEOF'
import json, os, platform, socket, sys, time
from pathlib import Path
from trackmanager import config, gsheets
from trackmanager.store import iso
cfg = config.load(sys.argv[1], check_paths=False)
out = {"ts": iso(), "session": os.environ.get("skaha_sessionid", "nosession"), "host": socket.gethostname(),
       "python": platform.python_version(), "results": {}}
try:
    import google.auth
    out["google_auth"] = google.auth.__version__
    out["service_account"] = gsheets.service_account_email(cfg.sheets_key)
    for which, sid in (("tracker", cfg.sheets.tracker_id), ("flagbook", cfg.sheets.flagbook_id)):
        t0 = time.time()
        try:
            m = gsheets.SheetsClient(sid, credentials=cfg.sheets_key).meta("properties(title),sheets(properties(title))")
            out["results"][which] = {"ok": True, "s": round(time.time() - t0, 2), "title": m["properties"]["title"],
                                     "tabs": [s["properties"]["title"] for s in m["sheets"]]}
        except gsheets.SheetsError as e:
            out["results"][which] = {"ok": False, "error": str(e)}
except Exception as e:
    out["error"] = repr(e)
out["ok"] = bool(out["results"]) and all(r["ok"] for r in out["results"].values())
path = cfg.root / "probes" / f"sheets-{out['session']}.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(out, indent=1) + "\n")
print(json.dumps(out, indent=1)); print(f"wrote {path}")
sys.exit(0 if out["ok"] else 1)
PYEOF
