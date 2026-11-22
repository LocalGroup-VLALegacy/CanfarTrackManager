"""STAGING: put the track's raw SDM on /arc and verify it (plan §3.4 `stage_local`).

    submit(ctl, st)      one flexible session per attempt: trackmanager/stage_worker.py symlinks an
                         SDM directory, or unpacks an NRAO tar, into tracks/<EB>/raw/ and writes
                         tracks/<EB>/stage/manifest.json
    reconcile(ctl, st)   watch it (stages/workers.py), then verify()
    verify(ctl, st, att) the manifest says ok, the SDM is there, and the intents in Scan.xml agree with
                         how the project cfgs classify each field; then STAGING -> CALIBRATING (queued)

Staging has its own attempt counter (`stage_attempt`; files under tracks/<EB>/stage/), because it
happens once per track, before iteration 1. A manifest that says "not ok" is a verdict about the
data, so it goes to the operator without an automatic retry; a missing manifest (killed session,
lost record) is retried up to controller.auto_retry times.
"""
from __future__ import annotations

import os
import shutil

from .. import cfgs, preflight
from .. import states as S
from ..backend import LIVE
from ..store import iso, read_json, write_json
from . import workers
from .workers import Attempt, classify, create, platform_text, submit_session

TARGET_INTENT = "OBSERVE_TARGET"


def attempt_ref(ctl, st, k=None) -> Attempt:
    eb = st["eb"]
    k = st.get("stage_attempt", 0) if k is None else k
    return Attempt("stage", eb, ctl.r.stage_attempt(eb, k), ctl.r.stage_logs(eb, k), None, k, S.STAGING)


def input_path(ctl, st):
    spec = dict(ctl.spec(st))
    if st.get("spec", {}).get("sdm"):
        spec.setdefault("sdm", st["spec"]["sdm"])
    return ctl.cfg.input_path(spec)


def submit(ctl, st):
    eb, spec = st["eb"], ctl.spec(st)
    k = st.get("stage_attempt", 0) + 1
    path = input_path(ctl, st)
    if path is None or not path.exists():
        # nothing to run a session for; the operator fixes the config or the data, then `retry`
        what = "no input: set inputs.arc_dir (archive tracks) or the track's path" if path is None \
            else f"input {path} does not exist"
        st["stage_attempt"] = k
        return ctl.fail(st, f"staging cannot start: {what}", "failed")
    (ctl.r.track(eb) / "worker.lock").unlink(missing_ok=True)   # every earlier attempt is terminal
    sw = ctl.cfg.pipeline.stage_worker
    env = {"TM_ROOT": str(ctl.r.path), "EB": eb, "ATTEMPT": str(k), "INPUT_PATH": str(path),
           "SIZE_TOLERANCE": str(sw.size_tolerance),
           "MODE": spec.get("stage_mode", "ok"), "SLEEP_S": str(spec.get("stage_sleep_s", 0))}
    sdm = (st.get("spec") or {}).get("sdm") or spec.get("sdm")
    if sdm:
        env["EXPECT_SDM"] = sdm
    size = st.get("est_size_gb") or spec.get("size_gb")
    if size:
        env["EST_BYTES"] = str(int(float(size) * 1e9))
    att = {"name": ctl.session_name(eb, None, k), "kind": "stage", "attempt": k, "requested_at": iso(),
           "request": {"image": ctl.cfg.stage_image(), "cmd": sw.cmd, "args": ctl.cfg.stage_worker_args(),
                       "env": env, "cores": None, "ram": None},
           "ids": None, "last_status": None, "platform_saved": False}
    a = attempt_ref(ctl, st, k)
    st["stage_attempt"] = k
    if not create(a, att):
        return ctl.set_activity(st, S.RUNNING, f"found existing staging attempt {k}")
    ctl.set_activity(st, S.RUNNING, f"submitting staging attempt {k}")
    submit_session(ctl, a, att, env)


def reconcile(ctl, st):
    workers.reconcile(ctl, st, attempt_ref(ctl, st), verify)


def intent_problems(fields: dict, tables) -> list[str]:
    """Compare how the cfgs classify each field by name with what the scans say it was observed as.
    A target by name needs an OBSERVE_TARGET scan; a calibrator (or unrecognised field) must not
    have one. Calibrators without a CALIBRATE intent are fine (system-configuration scans etc.)."""
    kinds = preflight.classify(list(fields), tables)
    out = []
    for name, info in fields.items():
        observed_target = TARGET_INTENT in info["intents"]
        if kinds[name] == "target" and not observed_target:
            out.append(f"{name} matches a target in the cfgs but has no {TARGET_INTENT} scan "
                       f"(intents: {', '.join(info['intents']) or 'none'})")
        elif kinds[name] != "target" and observed_target:
            out.append(f"{name} was observed as {TARGET_INTENT} but the cfgs call it {kinds[name]}")
    return out


def _tables(ctl):
    cache = ctl.cycle_cache
    if "stage_tables" not in cache:
        cache["stage_tables"] = cfgs.load(ctl.cfg) if ctl.cfg.pipeline.source_configs else None
    return cache["stage_tables"]


def check_manifest(ctl, st, att):
    """(manifest or None, problems). None means no verdict yet: no manifest from this attempt."""
    eb = st["eb"]
    man = read_json(ctl.r.manifest(eb))
    if man is None:
        return None, ["no manifest.json"]
    if man.get("ok"):
        if not man.get("sdm_path") or not os.path.exists(man["sdm_path"]):
            return man, [f"manifest ok but {man.get('sdm_path')} is gone"]
        try:
            tables = _tables(ctl)
        except cfgs.CfgError as e:
            return man, [f"cannot check intents: {e}"]
        return man, intent_problems(man.get("fields") or {}, tables) if tables else []
    if (man.get("finished") or "") < att["requested_at"]:
        return None, ["no manifest.json from this attempt"]       # an earlier attempt's verdict
    return man, list(man.get("problems") or ["manifest not ok"])


def verify(ctl, st, att):
    eb, k = st["eb"], st.get("stage_attempt", 0)
    a = attempt_ref(ctl, st)
    run = read_json(a.logs / "run.json") or {}
    man, problems = check_manifest(ctl, st, att)
    common = {"platform_status": att.get("platform_status"), "exit_code": run.get("exit_code"),
              "wall_s": run.get("wall_s"),
              "peak_rss_mb": run.get("peak_rss_mb")}
    if man is not None and not problems:
        att["outcome"] = "completed"
        write_json(a.path, att)
        a.job_row(ctl, att, status="verified", failure_class="none", **common)
        shutil.rmtree(ctl.r.raw(eb) / ".partial", ignore_errors=True)
        st.update(sdm=man["sdm"], sdm_bytes=man.get("bytes"))
        if man.get("ratio") is not None:
            st["size_ratio"] = man["ratio"]
        ctl.set_stage(st, S.CALIBRATING, S.QUEUED,
                      f"raw data staged ({man['mode']}, {(man.get('bytes') or 0) / 1e9:.1f} GB, "
                      f"{len(man.get('fields') or {})} fields)")
        return
    fclass = classify(att.get("platform_status"), run, platform_text(a))
    att["outcome"] = fclass
    write_json(a.path, att)
    a.job_row(ctl, att, status="failed_verification", failure_class=fclass, **common)
    if man is not None:      # a verdict about the data: retrying would give the same answer
        return ctl.fail(st, f"staging attempt {k}: {'; '.join(problems)}", fclass)
    live_same_name = any(s["status"] in LIVE for s in ctl.by_name(att["name"]))
    if k <= ctl.cfg.controller.auto_retry and not live_same_name:
        ctl.set_activity(st, S.QUEUED, f"staging attempt {k} {fclass}; automatic retry")
    else:
        ctl.fail(st, f"staging attempt {k} {fclass}; operator decision needed", fclass)
