"""The calibrate worker stage: submit a session, watch it, verify its outputs (plan §3.3).

Each function takes the controller (`ctl`) for its database, backend, config and event log."""
from __future__ import annotations
from .. import qa, retention
from .. import states as S
from ..backend import LIVE
from ..store import iso, read_json, write_json
from . import qa_publish
from .workers import Attempt, classify, create, peak_anon, platform_text, submit_session  # noqa: F401 (classify re-exported)
from . import workers


def attempt_ref(ctl, st, k=None) -> Attempt:
    eb, n = st["eb"], st.get("iteration", 1)
    k = st["attempt"] if k is None else k
    return Attempt("calibrate", eb, ctl.r.attempt(eb, n, k), ctl.r.logs(eb, n, k), n, k, S.CALIBRATING)


def submit(ctl, st):
    """Submit the next attempt. Returns a reason string (and submits nothing) when the track has to
    wait: with the Sheets bridge on, a run whose inputs no verdict froze (iteration 1, or the run
    after a reopen) first freezes the ticked rows of the track's flag sheet."""
    eb = st["eb"]
    spec = ctl.spec(st)
    n, k = st.get("iteration", 1), st["attempt"] + 1
    if k == 1 and ctl.bridge and not (ctl.r.inputs(eb, n) / "manual_flagging.txt").exists():
        wait = ctl.bridge.run_inputs(st)
        if wait:
            return wait
    lock = ctl.r.track(eb) / "worker.lock"
    lock.unlink(missing_ok=True)  # every earlier attempt is terminal by now
    ctl.r.inputs(eb, n).mkdir(parents=True, exist_ok=True)
    if k == 1 and n > 1:
        retention.drop_previous_work(ctl, eb, n - 1)
    res = ctl.cfg.resources(spec)
    env = {"TM_ROOT": str(ctl.r.path), "EB": eb, "ITERATION": str(n), "ATTEMPT": str(k),
           "MODE": spec.get("mode", "ok"), "SLEEP_S": str(spec.get("sleep_s", 60)),
           "TRIM_WORK": "1" if ctl.retention("trim_work") else "0",
           "QA_DIR": str(qa.qa_dir(ctl, eb, n)), "QA_NAV_FIX": "1"}
    # CASA, QA and pipeline paths, OMP_NUM_THREADS and per-project env come from project.yaml.
    env.update(ctl.cfg.worker_env(spec))
    att = {"name": ctl.session_name(eb, n, k), "iteration": n, "attempt": k, "requested_at": iso(),
           "request": {"image": ctl.cfg.pipeline.image, "cmd": ctl.cfg.pipeline.worker.cmd,
                       "args": ctl.cfg.worker_args(), "env": env,
                       "cores": res.cores, "ram": res.ram},
           "ids": None, "last_status": None, "platform_saved": False}
    a = attempt_ref(ctl, st, k)
    if not create(a, att):
        st["attempt"] = k
        ctl.set_activity(st, S.RUNNING, f"found existing iteration {n} attempt {k}")
        return
    st["attempt"] = k
    ctl.set_activity(st, S.RUNNING, f"submitting iteration {n} attempt {k}")
    submit_session(ctl, a, att, env)


def reconcile(ctl, st):
    workers.reconcile(ctl, st, attempt_ref(ctl, st), verify)


def verify(ctl, st, att):
    eb, n, k = st["eb"], st.get("iteration", 1), st["attempt"]
    idir = ctl.r.iter_dir(eb, n)
    res = read_json(ctl.r.result(eb, n))
    run = read_json(ctl.r.logs(eb, n, k) / "run.json") or {}
    ok = bool(res and res.get("ok") and res.get("iteration", n) == n
              and all((idir / o).exists() for o in res.get("outputs", [])))
    peak = run.get("peak_anon_bytes")
    common = {"platform_status": att.get("platform_status"),
              "exit_code": run.get("exit_code"), "wall_s": run.get("wall_s"),
              "peak_rss_mb": round(peak / 2**20) if peak else run.get("peak_rss_mb"),
              "peak_disk_gb": round(run["peak_workdir_bytes"] / 1e9, 1)
              if run.get("peak_workdir_bytes") else None}
    if ok:
        att["outcome"] = "completed"
        write_json(ctl.r.attempt(eb, n, k), att)
        ctl.job_row(eb, n, k, att, status="verified", failure_class="none", **common)
        retention.trim_work(ctl, eb, n)
        ctl.set_activity(st, S.PUBLISHING, f"iteration {n} attempt {k} verified "
                                           f"(platform: {common['platform_status']})")
        qa_publish.run(ctl, st)
        return
    a = attempt_ref(ctl, st)
    fclass = classify(att.get("platform_status"), run, platform_text(a), peak_anon(a),
                      att.get("request", {}).get("ram"))
    att["outcome"] = fclass
    write_json(ctl.r.attempt(eb, n, k), att)
    ctl.job_row(eb, n, k, att, status="failed_verification", failure_class=fclass, **common)
    live_same_name = any(s["status"] in LIVE for s in ctl.by_name(att["name"]))
    if k <= ctl.cfg.controller.auto_retry and not live_same_name:
        ctl.set_activity(st, S.QUEUED, f"iteration {n} attempt {k} {fclass}; automatic retry")
    else:
        ctl.fail(st, f"iteration {n} attempt {k} {fclass}; operator decision needed", fclass)
