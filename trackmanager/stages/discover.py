"""DISCOVERED: the archive sync that creates tracks, and preflight that decides where each goes.

    sync(ctl)   once per archive.poll_h: query TAP, write tracks/<EB>/archive.json, start new
                EBs at DISCOVERED, log changes and missing EBs as events (plan §3.7)
    run(ctl,st) preflight for a DISCOVERED track (HANDLERS[DISCOVERED]), then
                  preflight fails       -> AWAITING_DECISION, reason preflight_failed
                                           (operator fixes the cfgs and sends `recheck`)
                  skip candidate        -> AWAITING_DECISION, reason skip_candidate
                                           (operator sends `include` or `skip`)
                  ok, nrao_link         -> AWAITING_DATA, request_state needs_request  (M6 acts)
                  ok, arc_path          -> STAGING, queued                             (M3 acts)
"""
from __future__ import annotations

from .. import archive, cfgs, preflight
from .. import states as S
from ..store import log, read_json


def sync(ctl):
    cfg = ctl.cfg
    if not (archive.enabled(cfg) and archive.due(ctl.r, cfg)):
        return
    res = archive.sync(ctl.r, cfg)
    if not res.ok:
        n = (read_json(ctl.r.archive_dir / "sync.json") or {}).get("consecutive_failures", 1)
        log(f"archive sync failed ({n} in a row): {res.error}")
        if n == 1 or n == archive.ALARM_AFTER:
            ctl.event("-", "archive", "archive", f"sync failed ({n} in a row): {res.error}")
        return
    for eb in res.new:
        if read_json(ctl.r.state(eb)) is None:
            rec = res.kept[eb]
            st = {"eb": eb, "stage": "NEW", "iteration": 1, "attempt": 0, "hold": False,
                  "spec": {"config": rec["config"], "source": "archive"}}
            ctl.set_stage(st, S.DISCOVERED, None, "found by archive sync")
    for eb, diff in res.changed.items():
        st = read_json(ctl.r.state(eb)) or {}
        stage = st.get("stage", "-")
        text = ", ".join(f"{k} {a} -> {b}" for k, (a, b) in diff.items() if k not in ("start", "end"))
        ctl.event(eb, stage, stage, f"archive metadata changed: {text or 'times'}", archive_changed=diff)
    for eb in res.missing:
        st = read_json(ctl.r.state(eb)) or {}
        ctl.event(eb, st.get("stage", "-"), st.get("stage", "-"),
                  "missing from the latest archive result (kept; never removed automatically)")
    if res.new or res.changed or res.missing:
        log(f"archive sync: {len(res.new)} new, {len(res.changed)} changed, {len(res.missing)} missing, "
            f"{len(res.excluded)} excluded, {len(res.kept)} EBs")


def _tables(ctl):
    """The cfgs and the project's main setup, read once per cycle for all DISCOVERED tracks."""
    cache = ctl.cycle_cache
    if "preflight" not in cache:
        tables = cfgs.load(ctl.cfg)
        kept = (read_json(ctl.r.archive_dir / "index.json") or {}).get("kept", [])
        recs = [r for r in (read_json(ctl.r.archive_json(e)) for e in kept) if r]
        cache["preflight"] = (tables, preflight.main_signature(recs))
    return cache["preflight"]


def run(ctl, st):
    eb = st["eb"]
    rec = read_json(ctl.r.archive_json(eb))
    if rec is None:
        st.update(reason="preflight_failed", failed_from=S.DISCOVERED, failed_activity=None)
        return ctl.set_stage(st, S.AWAITING_DECISION, None, f"no archive record for {eb}: run an archive sync")
    try:
        tables, main_sig = _tables(ctl)
    except cfgs.CfgError as e:
        st.update(reason="preflight_failed", failed_from=S.DISCOVERED, failed_activity=None)
        return ctl.set_stage(st, S.AWAITING_DECISION, None, f"preflight cannot read the cfgs: {e}")
    included = bool(st.get("included")) or any(
        str(x) in (rec["sdm"], str(rec["ebid"])) for x in ctl.cfg.archive.include_ebs)
    res = preflight.run(rec, ctl.cfg, tables, included=included, main_sig=main_sig)
    st["preflight"] = res.to_dict()
    st["est_size_gb"] = rec["est_size_gb"]
    if res.verdict == "preflight_failed":
        st.update(reason="preflight_failed", failed_from=S.DISCOVERED, failed_activity=None)
        return ctl.set_stage(st, S.AWAITING_DECISION, None, f"preflight failed: {res.summary()}")
    if res.verdict == "skip_candidate":
        st.update(reason="skip_candidate")
        st.pop("failed_from", None)
        return ctl.set_stage(st, S.AWAITING_DECISION, None, f"skip candidate: {res.summary()}")
    for key in ("reason", "failed_from", "failed_activity", "failure_class"):
        st.pop(key, None)
    note = f"preflight {res.summary()}" + (" (included by the operator)" if included else "")
    if ctl.cfg.inputs.source == "nrao_link":
        st.update(request_state="needs_request", archive_url=rec["archive_url"])
        return ctl.set_stage(st, S.AWAITING_DATA, None, f"{note}; the data has to be requested from NRAO")
    return ctl.set_stage(st, S.STAGING, S.QUEUED, f"{note}; raw data from ARC")
