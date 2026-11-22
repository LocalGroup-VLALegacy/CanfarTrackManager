"""Operator and reviewer input: JSON files dropped into inbox/decisions/ (plan §3.2).
The controller is the only writer of state/, so every action goes through here."""
from __future__ import annotations
import re

from . import review
from .states import REVIEW_ACTIONS
from .store import iso, log, read_json, write_json


def apply(ctl):
    for f in sorted(ctl.r.inbox.glob("*.json")) if ctl.r.inbox.exists() else []:
        d = read_json(f, {})
        eb, action = d.get("eb"), d.get("action")
        valid = bool(eb) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", eb) is not None
        st = read_json(ctl.r.state(eb)) if valid else None
        result = "ignored"
        if action == "request":
            if not valid:
                result = "ignored: invalid EB name"
            elif st:
                result = f"ignored: already tracked ({st['stage']})"
            else:
                spec = {k: v for k, v in (d.get("spec") or {}).items() if k != "eb"}
                st = {"eb": eb, "stage": "NEW", "attempt": 0, "hold": False, "spec": spec,
                      "requested_by": d.get("by", "?")}
                ctl.set_stage(st, "QUEUED", f"requested by {d.get('by', '?')}")
                result = "applied"
        elif st and action in REVIEW_ACTIONS:
            result = review.apply(ctl, st, d)
        elif st and action == "retry" and st["stage"] == "AWAITING_DECISION":
            ctl.set_stage(st, "QUEUED", f"operator retry ({d.get('by', '?')})")
            result = "applied"
        elif st and action == "skip" and st["stage"] == "AWAITING_DECISION":
            ctl.set_stage(st, "SKIPPED", f"operator skip ({d.get('by', '?')})")
            result = "applied"
        elif st and action in ("hold", "release"):
            st["hold"] = action == "hold"
            write_json(ctl.r.state(eb), st)
            ctl.event(eb, st["stage"], st["stage"], f"operator {action}")
            result = "applied"
        ctl.r.inbox_archive.mkdir(parents=True, exist_ok=True)
        write_json(ctl.r.inbox_archive / f.name, {**d, "result": result, "processed": iso()})
        f.unlink()
        log(f"inbox {f.name}: {action} for {eb} -> {result}")
