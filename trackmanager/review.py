"""Reviewer verdicts (flag-tab M1 via decisions in inbox/): COMPLETE -> pass, RESTART -> restart;
MANUAL REVIEW and HELP REQUESTED hold the track for the operator (no rerun) until a later pass
or restart."""
from __future__ import annotations

import os

from . import states
from .stages import promote
from .store import iso, read_json, write_json


def apply(ctl, st, d):
    """A reviewer verdict for the current iteration: pass -> DONE, restart -> iteration n+1,
    manual_review / help_requested -> HOLD flag set (stage stays AWAITING_REVIEW), shown to the
    operator; a later pass or restart for the same iteration clears that hold."""
    eb, n, by = st["eb"], st.get("iteration", 1), d.get("by", "?")
    if d.get("iteration") is not None and int(d["iteration"]) != n:
        return f"ignored: decision for iteration {d['iteration']}, track is at iteration {n}"
    if st["stage"] != states.AWAITING_REVIEW:
        return f"ignored: track is {st['stage']}, not AWAITING_REVIEW"
    inputs = d.get("inputs") or {}
    bad = sorted(set(inputs) - set(states.INPUT_FILES))
    if bad:
        return f"ignored: unknown input files {bad} (allowed: {list(states.INPUT_FILES)})"
    review = {"iteration": n, "verdict": d["action"], "by": by, "decided": d.get("ts"),
              "applied": iso(), "notes": d.get("notes", ""), "inputs_next": sorted(inputs)}
    k = st.get("attempt") or 0
    att = read_json(ctl.r.attempt(eb, n, k)) if k else None
    if att:  # the job log shows which job's output each verdict was about
        ctl.job_row(eb, n, k, att, verdict=d["action"], verdict_at=review["applied"])
    if d["action"] in ("manual_review", "help_requested"):
        history = (read_json(ctl.r.review(eb, n)) or {}).get("history", [])
        write_json(ctl.r.review(eb, n), {**review, "history": history + [review]})
        st.update(hold=True, hold_reason=d["action"])
        write_json(ctl.r.state(eb), st)
        ctl.event(eb, st["stage"], st["stage"], f"{d['action']} from {by}: held for the operator"
                   + (f" ({d['notes']})" if d.get("notes") else ""))
        return "applied"
    history = (read_json(ctl.r.review(eb, n)) or {}).get("history", [])
    write_json(ctl.r.review(eb, n), {**review, "history": history + [review]})
    if st.get("hold_reason") in ("manual_review", "help_requested"):
        st.update(hold=False, hold_reason=None)  # the reviewer has now decided
    if d["action"] == "pass":
        ctl.set_stage(st, states.PROMOTING, None, f"iteration {n} passed review ({by})")
        promote.run(ctl, st)
        return "applied"
    nxt = ctl.r.inputs(eb, n + 1)
    nxt.mkdir(parents=True, exist_ok=True)
    for name, text in inputs.items():  # frozen now; later sheet edits don't change this run
        tmp = nxt / f"partial-{name}"
        tmp.write_text(text)
        os.replace(tmp, nxt / name)
    st.update(iteration=n + 1, attempt=0)
    ctl.set_stage(st, states.CALIBRATING, states.QUEUED, f"restart requested by {by}: iteration {n + 1} "
                                 f"with {', '.join(sorted(inputs)) or 'no new inputs'}")
    return "applied"
