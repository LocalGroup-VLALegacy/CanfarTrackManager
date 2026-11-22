"""The calibrate worker stage: submit a session, watch it, verify its outputs (plan §3.3).

Each function takes the controller (`ctl`) for its database, backend, config and event log."""
from __future__ import annotations
import json
import shutil
from pathlib import Path

from .. import qa
from ..backend import LIVE, TERMINAL
from ..store import age_s, create_json, iso, log, read_json, write_json
from . import promote, qa_publish


def classify(status, run, platform_text):
    """failure_class for a failed attempt (plan §3.2 job database)."""
    text = (platform_text or "").lower()
    if run and run.get("exit_code") == 0:
        return "no_output"
    if run and run.get("exit_code") not in (None, 0):
        return "failed"
    if "oomkilled" in text or "out of memory" in text:
        return "oom"
    if "deadline" in text:
        return "deadline"
    if status in TERMINAL:
        return "failed"
    return "lost"


def submit(ctl, st):
    eb = st["eb"]
    spec = ctl.spec(st)
    n, k = st.get("iteration", 1), st["attempt"] + 1
    lock = ctl.r.track(eb) / "worker.lock"
    lock.unlink(missing_ok=True)  # every earlier attempt is terminal by now
    ctl.r.inputs(eb, n).mkdir(parents=True, exist_ok=True)
    if k == 1 and n > 1:
        drop_previous_work(ctl, eb, n - 1)
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
    try:
        create_json(ctl.r.attempt(eb, n, k), att)  # attempt file before any API call
    except FileExistsError:
        log(f"{eb}: iter_{n}/attempt_{k}.json already exists; reconciling it instead of resubmitting")
        st["attempt"] = k
        ctl.set_stage(st, "RUNNING", f"found existing iteration {n} attempt {k}")
        return
    st["attempt"] = k
    ctl.set_stage(st, "RUNNING", f"submitting iteration {n} attempt {k}")
    rq = att["request"]
    try:
        ids = ctl.backend.create(att["name"], rq["image"], rq["cmd"], rq["args"], env,
                                  rq["cores"], rq["ram"])
        if eb in ctl.lose_response:
            log(f"{eb}: simulating a lost create() response (discarding {ids})")
            ctl.lose_response.discard(eb)
            ids = []
    except Exception as e:
        att["create_error"] = repr(e)
        ids = []
    att["ids"] = ids or None
    att["created_at"] = iso()
    write_json(ctl.r.attempt(eb, n, k), att)
    ctl.job_row(eb, n, k, att, status="submitted" if ids else "submit_unconfirmed",
                 cores=rq["cores"], ram=rq["ram"], image=rq["image"])


def drop_previous_work(ctl, eb, prev):
    """retention.drop_prev_iter_work: a restart replaces iteration <prev>, so its splits/ and
    any work/ go; its products/, inputs/, logs/ and review.json stay as the record."""
    if not ctl.retention("drop_prev_iter_work"):
        return
    idir, res = ctl.r.iter_dir(eb, prev), read_json(ctl.r.result(eb, prev))
    if not (res and res.get("ok") and Path(res.get("qa_dir") or qa.qa_dir(ctl, eb, prev)).is_dir()):
        return
    gone = [d.name for d in intermediate_dirs(idir) + [idir / "splits"] if d.exists()]
    for name in gone:
        shutil.rmtree(idir / name, ignore_errors=True)
    if gone:
        ctl.event(eb, "retention", "dropped", f"iter_{prev}: deleted {', '.join(gone)} "
                   "(replaced by a restart; products kept)")


def intermediate_dirs(idir):
    """work/ plus work and products dirs that failed attempts left aside (*.stale.*)."""
    return [idir / "work"] + sorted(idir.glob("work.stale.*")) + sorted(idir.glob("products.stale.*"))


def trim_work(ctl, eb, n):
    """retention.trim_work, after a verified run. The worker trims too; this catches a worker
    that was killed after writing result.json and covers older workers."""
    if not ctl.retention("trim_work"):
        return
    idir = ctl.r.iter_dir(eb, n)
    gone = [d.name for d in intermediate_dirs(idir) if d.exists()]
    for name in gone:
        shutil.rmtree(idir / name, ignore_errors=True)
    if gone:
        ctl.event(eb, "retention", "trimmed", f"iter_{n}: deleted {', '.join(gone)}")


def reconcile(ctl, st):
    eb, n, k = st["eb"], st.get("iteration", 1), st["attempt"]
    att = read_json(ctl.r.attempt(eb, n, k))
    sess = None
    if att.get("ids"):
        sess = next((s for s in ctl.sessions if s["id"] == att["ids"][0]), None)
    if sess is None:
        cands = ctl.by_name(att["name"])
        sess = cands[0] if cands else None
        if sess and not att.get("ids"):
            att["ids"] = [sess["id"]]
            att["adopted"] = True
            write_json(ctl.r.attempt(eb, n, k), att)
            ctl.event(eb, "RUNNING", "RUNNING", f"adopted session {sess['id']} by name "
                       "after an unconfirmed create()")
            ctl.job_row(eb, n, k, att, status="adopted")
    status = sess["status"] if sess else None

    if status in LIVE:
        if status != att.get("last_status"):
            att["last_status"] = status
            att.setdefault("first_seen", {})[status] = iso()
            write_json(ctl.r.attempt(eb, n, k), att)
            ctl.job_row(eb, n, k, att, status=status)
        return
    if status in TERMINAL and not att.get("platform_saved"):
        save_platform(ctl, ctl.r.logs(eb, n, k), att, sess["id"])
        att["platform_status"] = status
    if status is None:
        if att.get("last_status") is None and (age_s(att["requested_at"]) or 0) < ctl.cfg.controller.grace_s:
            return  # never listed yet: create() may be accepted but not visible
        att["platform_status"] = att.get("platform_status") or "missing"
    att["last_status"] = status or "missing"
    write_json(ctl.r.attempt(eb, n, k), att)
    verify(ctl, st, att)


def save_platform(ctl, d, att, sid):
    d.mkdir(parents=True, exist_ok=True)
    errors = {}
    for name, fn, fname in (("info", ctl.backend.info, "session_info.json"),
                            ("events", ctl.backend.events, "events.txt"),
                            ("logs", ctl.backend.logs, "session.log")):
        try:
            out = fn(sid)
            text = out if isinstance(out, str) else json.dumps(out, indent=1, default=str)
            (d / fname).write_text(text or "")
        except Exception as e:
            errors[name] = repr(e)
    att["platform_saved"] = iso()
    if errors:
        att["platform_errors"] = errors


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
        trim_work(ctl, eb, n)
        qa_publish.run(ctl, st)
        to = "AWAITING_REVIEW" if ctl.cfg.review.enabled else "PROMOTING"
        ctl.set_stage(st, to, f"iteration {n} attempt {k} verified "
                               f"(platform: {common['platform_status']})")
        if to == "PROMOTING":
            promote.run(ctl, st)
        return
    text = ""
    for f in ("events.txt", "session_info.json", "session.log"):
        p = ctl.r.logs(eb, n, k) / f
        text += p.read_text(errors="ignore") if p.exists() else ""
    fclass = classify(att.get("platform_status"), run, text)
    att["outcome"] = fclass
    write_json(ctl.r.attempt(eb, n, k), att)
    ctl.job_row(eb, n, k, att, status="failed_verification", failure_class=fclass, **common)
    live_same_name = any(s["status"] in LIVE for s in ctl.by_name(att["name"]))
    if k <= ctl.cfg.controller.auto_retry and not live_same_name:
        ctl.set_stage(st, "QUEUED", f"iteration {n} attempt {k} {fclass}; automatic retry")
    else:
        ctl.set_stage(st, "AWAITING_DECISION",
                       f"iteration {n} attempt {k} {fclass}; operator decision needed")
