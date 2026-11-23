"""Controller: one per project, ideally as a headless CANFAR session.

Each cycle (plan §3.3) it:
  1. checks the lease and writes a heartbeat;
  2. makes one fetch() of all headless sessions (no state changes without a fresh listing);
  3. applies operator and reviewer input from inbox/ (inbox.py, review.py);
  4. runs the handler for every track's (stage, activity) (HANDLERS below): reconcile a running
     calibrate attempt (live -> wait; terminal -> save info/logs/events, then verify outputs;
     missing -> verify outputs, since records are kept ~1 h), the same for a stage_local attempt
     (STAGING: verify stage/manifest.json), publish QA, promote;
  5. submits queued tracks that scheduler.plan admits: attempt file first, then create(), then
     record the IDs. Per-stage max_workers, a shared max_sessions and one admit() gate decide;
     every live session with this project's worker prefix takes a slot, orphans included. The
     rest stay queued, with queue_position and blocked_by recorded in state/;
  6. writes state/summary.json and jobs/summary.json, and hands off to a successor session when
     controller.relaunch_after_d has passed.

Stages and their edges are in states.py (10 stages; progress inside a stage is the `activity`
field). Tracks come from project.yaml's `tracks` and from "request" actions in inbox/
(trackmanager.request); they enter at STAGING/queued when they name an input (`path`), else at CALIBRATING/queued.
With review.enabled, a verified iteration goes to AWAITING_REVIEW; the verdict is "pass"
(-> PROMOTING -> DONE) or "restart" (-> CALIBRATING/queued as iteration n+1, with the decision's flag
and refant files frozen in iter_<n+1>/inputs/). Failures go to AWAITING_DECISION and remember where
they failed; "retry" returns there. Attempts are numbered within an iteration:
tracks/<EB>/iter_<n>/attempt_<k>.json.

Retention (project.yaml "retention"; plan §3.2, retention.py):
  trim_work (default true)       after a verified run, delete iter_<n>/work/ (full MS, caltables,
                                 intermediate tables) and stale work dirs of failed attempts;
                                 only splits/, products/, inputs/ and logs/ stay. A failed
                                 attempt's work/ is kept for diagnosis until a later attempt verifies.
  drop_prev_iter_work (true)     when iteration n+1 starts, delete iteration n's splits/ (and work/).
  science_dir                    where PROMOTING moves the approved iteration's splits:
                                 <science_dir>/<EB>/ (default <root>/science; relative paths are
                                 taken from the root).
  keep_raw_after_done (false)    PROMOTING deletes tracks/<EB>/raw/ unless this is true. Raw data
                                 stays on disk through every iteration until the track passes.

QA products ("qa" in project.yaml; plan §5, qa.py): every iteration's products, QA pages included, are
written straight to <products_dir>/<EB>/iter_<n>/ (default <root>/products), outside the track
database, so a reviewer's link never changes. COMPLETE marks the approved iteration
(<EB>/approved.json) instead of moving anything. Reviewers read them in a browser through the
ARC files service (url_base + the path after /arc). Each cycle the controller writes
<products_dir>/index.html (all tracks) and <products_dir>/<EB>/index.html (all iterations).
  products_dir   (default "products")
  group          CADC group given read access (POSIX ACL, shown as "Read" in Storage
                 Management) to each new QA folder; default none (owner and project group only)
  url_base       default https://ws-uv.canfar.net/arc/files

Local run:    python -m trackmanager.controller --project project.yaml [--root <run root>]
Fake backend: add --backend fake (workers run as local subprocesses).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import archive
from . import config as projconfig
from . import disk, inbox, qa, scheduler
from . import jobs as joblog
from . import states as S
from .stages import calibrate, discover, promote, qa_publish, stage_local
from .backend import LIVE, make_backend
from .store import Root, age_s, append_jsonl, iso, log, read_json, write_json

REPO = Path(__file__).resolve().parent.parent
# What the cycle does for a track, by (stage, activity) or by stage. Submitting is separate:
# queued tracks of a worker stage go through SUBMITTERS once the scheduler admits them.
# M6 adds `fetch_raw` beside `stage_local` under STAGING.
HANDLERS = {S.DISCOVERED: discover.run,
            (S.STAGING, S.RUNNING): stage_local.reconcile,
            (S.CALIBRATING, S.RUNNING): calibrate.reconcile,
            (S.CALIBRATING, S.PUBLISHING): qa_publish.run,
            S.PROMOTING: promote.run}
SUBMITTERS = {S.STAGING: stage_local.submit, S.CALIBRATING: calibrate.submit}


def cert_info():
    pem = Path.home() / ".ssl" / "cadcproxy.pem"
    info = {"cert_path": str(pem)}
    if pem.exists():
        info["cert_mtime"] = iso(dt.datetime.fromtimestamp(pem.stat().st_mtime, dt.timezone.utc))
        try:
            out = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", str(pem)],
                                 capture_output=True, text=True, timeout=10).stdout
            info["cert_not_after"] = out.strip().split("=", 1)[-1]
        except (OSError, subprocess.SubprocessError) as e:
            info["cert_error"] = repr(e)
    return info


class Controller:
    def __init__(self, cfg: projconfig.ProjectConfig, root: Path, backend, me: str, lose_response=(),
                 config_path: str | None = None, backend_kind: str = "canfar"):
        self.cfg = cfg
        self.r = Root(root)
        self.backend = backend
        self.me = me
        self.lose_response = set(lose_response)
        self.config_path = config_path
        self.backend_kind = backend_kind
        self.tag = re.sub(r"[^a-z0-9]", "", self.r.path.name.lower())[:10] or "run"
        # Session names are <prefix><run tag>-...; the prefix also marks this project's workers
        # when counting slots (orphans included), so keep it unique per project space.
        self.prefix = cfg.project.session_prefix.lower()
        self.cycle_n = 0
        self.started = iso()
        self.sessions: list[dict] = []
        self.orphans: set[str] = set()
        self.workers: dict = {}
        self.disk: dict = {}
        self.cycle_cache: dict = {}  # per-cycle values shared by handlers (reset in cycle())

    # ---- database helpers ------------------------------------------------
    def event(self, eb, frm, to, reason, **extra):
        append_jsonl(self.r.events, {"ts": iso(), "actor": self.me, "eb": eb,
                                     "from": frm, "to": to, "reason": reason, **extra})

    def job_row(self, eb, n, k, att, **fields):
        ids = att.get("ids") or []
        append_jsonl(self.r.jobs, {"ts": iso(), "eb": eb, "iteration": n, "attempt": k, "kind": "calibrate",
                                   "name": att["name"], "job_id": ids[0] if ids else None,
                                   **({} if "log_dir" in fields else {"log_dir": str(self.r.logs(eb, n, k))}),
                                   **fields})

    def load_state(self, eb):
        st = read_json(self.r.state(eb))
        if st is None:
            st = {"eb": eb, "stage": "NEW", "iteration": 1, "attempt": 0, "hold": False}
            self.set_stage(st, *S.initial_stage(self.cfg, self.spec(st)), "discovered from config")
        return st

    def track_ids(self):
        """Every track in the database: config EBs first, then requested ones."""
        ebs = [e["eb"] for e in self.cfg.track_specs()]
        sdir = self.r.summary.parent
        extra = sorted(p.stem for p in sdir.glob("*.json") if p.name != "summary.json") \
            if sdir.exists() else []
        return ebs + [eb for eb in extra if eb not in ebs]

    def spec(self, st):
        return next((e for e in self.cfg.track_specs() if e["eb"] == st["eb"]), None) or st.get("spec") or {}

    def retention(self, key):
        return getattr(self.cfg.retention, key)

    def qa(self, key):
        return getattr(self.cfg.qa, key)

    def base_dir(self, key):
        """products_dir / science_dir (relative settings are taken from the root)."""
        return self.cfg.products_dir if key == "products_dir" else self.cfg.science_dir

    def out_dir(self, key, eb):
        """science_dir / products_dir for one EB (relative settings are taken from the root)."""
        return self.base_dir(key) / eb

    def is_worker_name(self, name):
        return bool(name) and name.startswith(f"{self.prefix}{self.tag}-") \
            and not re.search(r"-ctrl-\d+$", name)

    def set_stage(self, st, to, activity, reason):
        """Move a track to stage `to` (and activity within it). The edge must be in the table
        (states.TRANSITIONS); a brand-new track (stage NEW) may enter at any stage."""
        frm, frm_act = st["stage"], st.get("activity")
        if frm != "NEW":
            S.check(frm, to)
        S.check_activity(to, activity)
        st.update(stage=to, activity=activity, updated=iso(), detail=reason)
        for key in ("queue_position", "blocked_by"):
            st.pop(key, None)
        if activity == S.QUEUED:
            st["queued_at"] = st["updated"]
        write_json(self.r.state(st["eb"]), st)
        self.event(st["eb"], frm, to, reason, from_activity=frm_act, activity=activity)
        log(f"{st['eb']}: {frm}{'/' + frm_act if frm_act else ''} -> {to}{'/' + activity if activity else ''} ({reason})")

    def set_activity(self, st, activity, reason):
        """Progress within the current stage (queued -> running -> publishing): no table edge."""
        stage, frm_act = st["stage"], st.get("activity")
        S.check_activity(stage, activity)
        st.update(activity=activity, updated=iso(), detail=reason)
        for key in ("queue_position", "blocked_by"):
            st.pop(key, None)
        if activity == S.QUEUED:
            st["queued_at"] = st["updated"]
        write_json(self.r.state(st["eb"]), st)
        self.event(st["eb"], stage, stage, reason, from_activity=frm_act, activity=activity)
        log(f"{st['eb']}: {stage}/{frm_act} -> {stage}/{activity} ({reason})")

    def fail(self, st, reason, failure_class=None):
        """Any working stage -> AWAITING_DECISION (reason failed). Remembers where it failed, so
        `retry` goes back there: a failed promotion retries promotion, a failed publish retries
        publishing, a failed run is queued again."""
        st.update(failed_from=st["stage"], failed_activity=st.get("activity"), reason="failed",
                  failure_class=failure_class)
        self.set_stage(st, S.AWAITING_DECISION, None, reason)

    def retry(self, st, by, action="retry"):
        """AWAITING_DECISION (reason failed or preflight_failed) -> the stage it failed in.
        A failed preflight goes back to DISCOVERED and is checked again (`recheck`)."""
        to = st.get("failed_from") or S.CALIBRATING
        act = st.get("failed_activity")
        if act == S.RUNNING or (act is None and to in S.ACTIVITIES):
            act = S.QUEUED  # a failed run is submitted again; a failed publish is just retried
        for key in ("failed_from", "failed_activity", "reason", "failure_class", "publish_failures",
                    "raw_delete_failures"):
            st.pop(key, None)
        self.set_stage(st, to, act if to in S.ACTIVITIES else None, f"operator {action} ({by})")

    def session_name(self, eb, n, k):
        """<prefix><run tag>-<eb>-i<iteration>-a<attempt> for a calibrate attempt; staging (n is
        None) has no iteration: <prefix><run tag>-<eb>-stage-a<attempt>."""
        clean = re.sub(r"[^a-z0-9-]", "-", eb.lower()).strip("-")
        return f"{self.prefix}{self.tag}-{clean}-{'stage' if n is None else f'i{n}'}-a{k}"[:60]

    @staticmethod
    def worker_kind(name):
        """Which worker kind a session name belongs to (see session_name)."""
        return "stage" if re.search(r"-stage-a\d+$", name or "") else "calibrate"

    # ---- lease -----------------------------------------------------------
    def stale(self, lease):
        a = age_s(lease.get("heartbeat"))
        return a is None or a > 3 * self.cfg.controller.poll_s

    def holder_live(self, holder):
        sid = holder.split("-local")[0]
        if "-local" in holder:
            return True  # local holders can only be judged by heartbeat age
        try:
            return any(s["id"] == sid and s["status"] in LIVE for s in self.backend.fetch())
        except Exception:
            return True

    def acquire_lease(self):
        while True:
            lease = read_json(self.r.lease)
            if lease and self.me in lease.get("cancelled_successors", []):
                sys.exit(f"handoff to {self.me} was cancelled by {lease['holder']}; exiting")
            if (not lease or lease.get("released") or lease["holder"] == self.me
                    or self.stale(lease)):
                break
            if lease.get("handoff_to") == self.me:
                write_json(self.r.waiting(self.me), {"ts": iso(), "waiting_for": lease["holder"]})
                if not self.holder_live(lease["holder"]):
                    break
                log(f"waiting for {lease['holder']} to hand over the lease")
                time.sleep(15)
                continue
            sys.exit(f"lease held by {lease['holder']} (heartbeat {age_s(lease['heartbeat']):.0f}s "
                     "ago); refusing to start a second controller")
        prev = (lease or {}).get("holder")
        write_json(self.r.lease, {"holder": self.me, "started": self.started, "heartbeat": iso(),
                                  "handoff_to": None, "released": False, "predecessor": prev})
        time.sleep(2)
        if read_json(self.r.lease)["holder"] != self.me:
            sys.exit("lost a race for the lease; another controller took it")
        self.r.waiting(self.me).unlink(missing_ok=True)
        self.event("-", "lease", "held", f"acquired (predecessor {prev})")

    def check_lease_and_beat(self):
        lease = read_json(self.r.lease)
        if not lease or lease["holder"] != self.me:
            sys.exit(f"lease now held by {lease and lease['holder']}; exiting")
        lease["heartbeat"] = iso()
        write_json(self.r.lease, lease)
        alarm = archive.alarm(self.r)
        write_json(self.r.heartbeat, {"holder": self.me, "ts": iso(), "cycle": self.cycle_n,
                                      **({"archive_alarm": alarm} if alarm else {}),
                                      "host": socket.gethostname(), **cert_info()})

    # ---- one cycle -------------------------------------------------------
    def cycle(self):
        self.cycle_n += 1
        self.check_lease_and_beat()
        try:
            self.sessions = self.backend.fetch()
        except Exception as e:  # no transitions without a fresh listing
            log(f"fetch failed, skipping cycle: {e!r}")
            return
        self.cycle_cache = {}
        discover.sync(self)  # archive tracks (DISCOVERED) before requests and config tracks
        for e in self.cfg.track_specs():  # config tracks enter the queue before any requests
            self.load_state(e["eb"])
        inbox.apply(self)
        states = {eb: self.load_state(eb) for eb in self.track_ids()}
        for st in states.values():
            handler = HANDLERS.get((st["stage"], st.get("activity"))) or HANDLERS.get(st["stage"])
            if handler:
                handler(self, st)
        self.schedule(states)
        self.write_summaries(states)
        if not self.settled(states):
            self.maybe_handoff()

    def schedule(self, states):
        """Submit queued tracks that the scheduler admits; the rest wait in the database with
        their queue position and the gate that blocks them (trackmanager/scheduler.py)."""
        limits = self.cfg.reload_limits()
        tracks = list(states.values())
        expected = set()
        for st in tracks:
            if scheduler.is_running(st):
                ref = (stage_local if st["stage"] == S.STAGING else calibrate).attempt_ref(self, st)
                expected.add((read_json(ref.path) or {}).get("name"))
        orphans = {s["id"]: s["name"] for s in self.sessions
                   if s["status"] in LIVE and self.is_worker_name(s.get("name"))
                   and s["name"] not in expected}
        for sid in set(orphans) - self.orphans:
            self.event("-", "workers", "orphan", f"live worker session {sid} ({orphans[sid]}) is not "
                       "a current attempt in the database; it takes a slot until it ends")
        self.orphans = set(orphans)
        by_kind = {kind: sum(self.worker_kind(n) == kind for n in orphans.values())
                   for kind in S.WORKER_KINDS}
        gate = disk.Gate(tracks, limits, self.spec)
        plan = scheduler.plan(tracks, limits, by_kind, admit=gate.admit,
                              priority=lambda st: self.spec(st).get("priority", 0))
        self.disk = gate.summary()
        for st in plan.submit:
            submit = SUBMITTERS.get(st["stage"])
            if submit is None:  # a worker stage whose handler is not written yet
                plan.waiting[st["eb"]] = {"position": len(plan.waiting) + 1,
                                          "blocked_by": f"no handler for {st['stage']} in this build"}
                continue
            submit(self, st)
        for st in tracks:
            w = plan.waiting.get(st["eb"])
            want = (w["position"], w["blocked_by"]) if w else (None, None)
            if (st.get("queue_position"), st.get("blocked_by")) != want:
                st.pop("queue_position", None)
                st.pop("blocked_by", None)
                if w:
                    st.update(queue_position=w["position"], blocked_by=w["blocked_by"])
                write_json(self.r.state(st["eb"]), st)
        if plan.waiting:
            log(f"{len(plan.waiting)} track(s) queued; " + ", ".join(
                f"{plan.in_use.get(k, 0)}/{limits.max_workers.get(k, 0)} {k} slots in use"
                for k in S.WORKER_KINDS))
        running = sum(scheduler.is_running(st) for st in tracks if st["stage"] == S.CALIBRATING)
        self.workers = {"limit": limits.max_workers.get("calibrate", 0), "limits": limits.max_workers,
                        "max_sessions": limits.max_sessions, "in_use": plan.in_use.get("calibrate", 0),
                        "running_tracks": running, "in_use_by_kind": dict(plan.in_use),
                        "orphans": sorted(orphans),
                        "queued": len(plan.waiting), "held": plan.held}

    @staticmethod
    def settled(states):
        return bool(states) and all(st["stage"] in S.SETTLED for st in states.values())

    def cancel_successor(self, reason):
        """Destroy a requested successor that is no longer needed (default on a normal finish)."""
        lease = read_json(self.r.lease)
        succ = lease.get("handoff_to")
        if not succ:
            return
        try:
            ok = self.backend.destroy(succ)
        except Exception as e:
            ok = False
            log(f"destroy of successor {succ} failed: {e!r}")
        lease["cancelled_successors"] = lease.get("cancelled_successors", []) + [succ]
        lease["handoff_to"] = None
        write_json(self.r.lease, lease)
        self.r.waiting(succ).unlink(missing_ok=True)
        self.event("-", "lease", "successor_cancelled",
                   f"{succ} ({reason}; destroy {'accepted' if ok else 'not confirmed'})")

    def by_name(self, name):
        cands = [s for s in self.sessions if s.get("name") == name]
        return sorted(cands, key=lambda s: s.get("startTime") or "", reverse=True)

    # ---- submission ------------------------------------------------------

    # ---- monitoring and verification ------------------------------------

    # ---- operator input --------------------------------------------------

    # ---- summaries and handoff ------------------------------------------
    def write_summaries(self, states):
        write_json(self.r.summary, {"ts": iso(), "controller": self.me, "cycle": self.cycle_n,
                                    "workers": self.workers, "disk": self.disk,
                                    "archive": {**(read_json(self.r.archive_dir / "sync.json") or {}),
                                                "alarm": archive.alarm(self.r)} if archive.enabled(self.cfg) else None,
                                    "qa_home_url": qa.qa_url(self, self.base_dir("products_dir") / "index.html"),
                                    "ebs": {eb: {k: st.get(k) for k in
                                                 ("stage", "activity", "iteration", "attempt", "detail", "updated",
                                                  "hold", "hold_reason", "reason", "queued_at", "queue_position",
                                                  "blocked_by", "qa_url", "request_state", "est_size_gb",
                                                  "sdm_bytes", "stage_attempt")}
                                            for eb, st in states.items()}})
        try:
            qa.write_index(self, states)
        except OSError as e:
            log(f"QA index not written: {e!r}")
        jobs = joblog.write_views(self.r, self.cfg.name, iso())
        joblog.write_track_views(self.r, states, jobs, self.cfg.name, iso())

    def maybe_handoff(self):
        after = (self.cfg.controller.relaunch_after_d or 0) * 86400
        if not after:
            return
        lease = read_json(self.r.lease)
        succ = lease.get("handoff_to")
        if succ:
            if self.r.waiting(succ).exists():
                lease.update(released=True, released_at=iso())
                write_json(self.r.lease, lease)
                self.event("-", "lease", "released", f"handed over to {succ}")
                log(f"successor {succ} is waiting; lease released, exiting")
                sys.exit(0)
            if not any(s["id"] == succ and s["status"] in LIVE for s in self.sessions) \
                    and (age_s(lease.get("handoff_at")) or 0) > self.cfg.controller.grace_s:
                self.event("-", "lease", "handoff_failed", f"successor {succ} not live; will retry")
                lease["handoff_to"] = None
                write_json(self.r.lease, lease)
            return
        if (age_s(self.started) or 0) < after:
            return
        c = self.cfg.controller
        name = f"{self.prefix}{self.tag}-ctrl-{self.cycle_n}"
        args = f"{REPO}/scripts/run_controller.sh {self.config_path} {self.r.path}"
        try:
            env = {"TM_HANDOFF_FROM": self.me, "TM_BACKEND": self.backend_kind,
                   **{k: str(v) for k, v in self.cfg.controller.successor_env.items()}}
            if self.backend_kind == "fake":
                env["TM_PYTHON"] = sys.executable
            ids = self.backend.create(name, c.image, "bash", args, env, None, None)
        except Exception as e:
            log(f"successor create failed: {e!r}")
            return
        append_jsonl(self.r.jobs, {"ts": iso(), "kind": "controller", "eb": "-", "name": name,
                                   "job_id": ids[0] if ids else None, "image": c.image,
                                   "status": "submitted" if ids else "submit_unconfirmed",
                                   "log_dir": str(self.r.path / "controller")})
        if ids:
            lease.update(handoff_to=ids[0], handoff_at=iso())
            write_json(self.r.lease, lease)
            self.event("-", "lease", "handoff_requested", f"successor session {ids[0]} ({name})")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True, help="project.yaml (plan §3.1)")
    p.add_argument("--root", help="track database root; overrides project.root (created if needed)")
    p.add_argument("--backend", choices=["canfar", "fake"], default="canfar")
    p.add_argument("--max-cycles", type=int, default=0, help="stop after N cycles (0 = until all settle)")
    p.add_argument("--lose-response", default="", help="comma-separated EBs whose create() response is discarded")
    a = p.parse_args(argv)

    try:  # the fake backend runs placeholder workers, so CASA paths are not checked
        cfg = projconfig.load(a.project, root=a.root, check_paths=a.backend != "fake")
    except projconfig.ConfigError as e:
        sys.exit(str(e))
    root = cfg.root
    root.mkdir(parents=True, exist_ok=True)
    sid = os.environ.get("skaha_sessionid", "nosession")
    me = sid if os.environ.get("TM_HEADLESS") else f"{sid}-local{os.getpid()}"
    backend = make_backend(a.backend, root)
    ctl = Controller(cfg, root, backend, me, [e for e in a.lose_response.split(",") if e],
                     cfg.path, a.backend)
    ctl.acquire_lease()
    ctl.event("-", "controller", "start", json.dumps(
        {"host": socket.gethostname(), "python": platform.python_version(), "backend": a.backend,
         "handoff_from": os.environ.get("TM_HANDOFF_FROM"), **cert_info()}))
    while True:
        ctl.cycle()
        stages = {st["stage"] for st in (read_json(ctl.r.summary) or {}).get("ebs", {}).values()}
        settled = stages and stages <= S.SETTLED
        if (a.max_cycles and ctl.cycle_n >= a.max_cycles) or (settled and not cfg.controller.keep_running
                                           and not archive.enabled(cfg)):
            log(f"stopping after cycle {ctl.cycle_n}: stages {sorted(stages)}")
            if cfg.controller.cancel_successor_on_finish:
                ctl.cancel_successor("work finished")
            lease = read_json(ctl.r.lease)
            lease.update(released=True, released_at=iso())
            write_json(ctl.r.lease, lease)
            ctl.event("-", "lease", "released", "controller finished normally")
            break
        time.sleep(cfg.controller.poll_s)


if __name__ == "__main__":
    main()
