"""Operator action `reopen` (decision 2026-11-24): a DONE track runs again, e.g. after a COMPLETE
set by mistake. Raw data is deleted at DONE, so the track is staged again first.

    python -m trackmanager.decide --root <run> --eb <EB> --action reopen --notes "completed by mistake"

What happens, all in one cycle:
  - the promoted science splits (<science_dir>/<EB>/) are renamed to <EB>.superseded-<ts>/ and the
    approved-iteration marker (<products_dir>/<EB>/approved.json) to approved.superseded-<ts>.json,
    so the next promotion never meets or overwrites them; nothing is deleted;
  - the completion record moves into st["reopened"] (a list: a track can be reopened again);
  - the track goes to iteration n+1 and on to the first stage that can run it:
      raw/ still on disk (retention.keep_raw_after_done)       -> CALIBRATING / queued
      its input is on /arc (track `path`, or inputs.arc_dir)   -> STAGING / queued (old manifest kept aside)
      nrao_link source                                         -> AWAITING_DATA, needs_request
      no input at all (placeholder tracks)                     -> CALIBRATING / queued
  - the next run's flags: with the Sheets bridge, the ticked rows of the track's flag sheet when the
    run is submitted (the sheet stayed editable after DONE, so it holds the last restart's rows plus
    any edits); without it, the approved iteration's frozen inputs are carried forward.
"""
from __future__ import annotations

import os
import shutil

from . import states as S
from .store import iso, read_json, write_json

COMPLETION = ("completed_at", "completed_iteration", "completed_attempt", "completed_by",
              "science_dir", "products_dir", "raw_deleted_at")


def _aside(path, stamp, suffix=""):
    """Rename path to <name>.superseded-<stamp><suffix>; return the new path or None."""
    if not path.exists():
        return None
    dst = path.with_name(f"{path.stem if suffix else path.name}.superseded-{stamp}{suffix}")
    os.replace(path, dst)
    return str(dst)


def apply(ctl, st, d) -> str:
    eb, n, by = st["eb"], st.get("iteration", 1), d.get("by", "?")
    if st["stage"] != S.DONE:
        return f"ignored: reopen applies to DONE tracks, not {st['stage']}"
    stamp = iso().replace(":", "").replace("-", "")
    moved = {"science": _aside(ctl.out_dir("science_dir", eb), stamp),
             "approved": _aside(ctl.out_dir("products_dir", eb) / "approved.json", stamp, ".json")}
    record = {"at": iso(), "by": by, "notes": d.get("notes", ""), "from_iteration": n,
              **{k: st.pop(k) for k in COMPLETION if k in st}, "superseded": moved}
    st.setdefault("reopened", []).append(record)
    st.update(iteration=n + 1, attempt=0)
    for key in ("hold", "hold_reason"):
        st.pop(key, None)
    st["hold"] = False
    if not ctl.bridge:                    # no flag sheet to read: carry the approved run's flags forward
        src, dst = ctl.r.inputs(eb, n), ctl.r.inputs(eb, n + 1)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                shutil.copy2(f, dst / f.name)
    spec = ctl.spec(st)
    raw = ctl.r.raw(eb)
    inp = ctl.cfg.input_path(spec, (read_json(ctl.r.archive_json(eb)) or {}).get("sdm") or spec.get("sdm"))
    why = f"operator reopen ({by}" + (f": {d['notes']}" if d.get("notes") else "") + f"): iteration {n + 1}"
    if raw.is_dir() and any(raw.iterdir()):
        ctl.set_stage(st, S.CALIBRATING, S.QUEUED, f"{why}; raw data still on disk")
    elif inp is not None and inp.exists():
        man = ctl.r.manifest(eb)
        if man.exists():
            record["manifest_before"] = _aside(man, stamp, ".json")
            write_json(ctl.r.state(eb), st)
        ctl.set_stage(st, S.STAGING, S.QUEUED, f"{why}; staging the raw data again from {inp}")
    elif ctl.cfg.inputs.source == "nrao_link" and (read_json(ctl.r.archive_json(eb)) or {}):
        st.update(request_state="needs_request",
                  archive_url=(read_json(ctl.r.archive_json(eb)) or {}).get("archive_url"))
        ctl.set_stage(st, S.AWAITING_DATA, None, f"{why}; the raw data has to be requested from NRAO again")
    else:
        ctl.set_stage(st, S.CALIBRATING, S.QUEUED, f"{why}; no raw input to stage")
    return "applied"
