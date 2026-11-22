"""Write an operator or reviewer decision into inbox/ (what the dashboard and Sheets bridge will do).

    python -m trackmanager.decide --root ../runs/r1 --eb bad --action retry
    python -m trackmanager.decide --root ../runs/r1 --eb X --action restart \\
        --flags my_flags.txt --refant-ignore ea26 --notes "RFI in scan 5"
    python -m trackmanager.decide --root ../runs/r1 --eb X --action pass
Actions: retry | skip | needs_manual (only applied in AWAITING_DECISION; skip also in
AWAITING_DATA), hold | release (any stage),
pass | restart | manual_review | help_requested (only applied in AWAITING_REVIEW, and only
to the iteration it names; by default the track's current iteration, read from state/).
manual_review and help_requested put the track on HOLD for the operator, with no rerun. A restart carries the full text
of the flag and refant files, so the controller freezes exactly what was decided.
"""
import argparse
import getpass
import re
from pathlib import Path

from .store import Root, create_json, iso, read_json


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True)
    p.add_argument("--eb", required=True)
    p.add_argument("--action", required=True,
                   choices=["retry", "skip", "needs_manual", "hold", "release", "pass", "restart",
                            "manual_review", "help_requested"])
    p.add_argument("--iteration", type=int, help="iteration the verdict is for (default: current)")
    p.add_argument("--flags", type=Path, help="manual_flagging.txt for the next iteration (restart)")
    p.add_argument("--refant-ignore", help="antennas to avoid as refant, e.g. ea26,ea10 (restart)")
    p.add_argument("--notes", default="")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", a.eb):
        p.error(f"invalid EB name {a.eb!r} (letters, digits, '.', '_', '-')")
    if a.action != "restart" and (a.flags or a.refant_ignore):
        p.error("--flags and --refant-ignore go with --action restart")
    r = Root(Path(a.root).resolve())
    d = {"eb": a.eb, "action": a.action, "by": getpass.getuser(), "ts": iso()}
    if a.action in ("pass", "restart", "manual_review", "help_requested"):
        st = read_json(r.state(a.eb)) or {}
        d["iteration"] = a.iteration or st.get("iteration", 1)
    if a.notes or a.action in ("pass", "restart", "manual_review", "help_requested"):
        d["notes"] = a.notes
    if a.action == "restart":
        inputs = {}
        if a.flags:
            inputs["manual_flagging.txt"] = a.flags.read_text()
        if a.refant_ignore:
            if not re.fullmatch(r"ea\d\d(,ea\d\d)*", a.refant_ignore):
                p.error("--refant-ignore must look like ea26 or ea26,ea10")
            inputs["refant_ignore.txt"] = a.refant_ignore + "\n"
        d["inputs"] = inputs
    path = r.inbox / f"{a.eb}.{iso().replace(':', '')}.json"
    create_json(path, d)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
