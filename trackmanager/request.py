"""Request a new track: it joins the queue in the database (CALIBRATING, activity queued) at the next cycle.

    python -m trackmanager.request --root ../runs/r1 --eb new-eb [--priority 1] [--env KEY=VALUE ...]
Placeholder-worker options: --mode ok|fail|fail_first|no_result|oom, --sleep-s N.

The request goes through inbox/ like any operator action, so the controller stays the only
writer of state/. A request for a track that is already in the database is archived as
ignored. Requested tracks wait queued until a worker slot is free (controller.max_workers), in
order of priority (higher first), then of the time they were queued.
"""
import argparse
import getpass
import re
from pathlib import Path

from .store import Root, create_json, iso


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True)
    p.add_argument("--eb", required=True)
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--mode")
    p.add_argument("--sleep-s", type=float)
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="worker environment for this track (added to the config's worker env)")
    a = p.parse_args(argv)
    spec = {"priority": a.priority}
    if a.mode:
        spec["mode"] = a.mode
    if a.sleep_s is not None:
        spec["sleep_s"] = a.sleep_s
    if a.env:
        spec["env"] = dict(kv.split("=", 1) for kv in a.env)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", a.eb):
        p.error(f"invalid EB name {a.eb!r} (letters, digits, '.', '_', '-')")
    r = Root(Path(a.root).resolve())
    path = r.inbox / f"{a.eb}.request.{iso().replace(':', '')}.json"
    create_json(path, {"eb": a.eb, "action": "request", "spec": spec, "by": getpass.getuser(), "ts": iso()})
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
