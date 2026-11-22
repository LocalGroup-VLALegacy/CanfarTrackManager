"""Controller: one per project, ideally as a headless CANFAR session.

Each cycle (plan §3.3) it:
  1. checks the lease and writes a heartbeat;
  2. makes one fetch() of all headless sessions (no state changes without a fresh listing);
  3. applies operator decisions from inbox/;
  4. reconciles every RUNNING EB: live -> wait; terminal -> save info/logs/events,
     then verify outputs; missing -> verify outputs (records are kept ~1 h);
  5. submits QUEUED EBs, oldest first (higher "priority" first), while fewer than
     max_workers worker sessions are live: attempt file first, then create(), then
     record the IDs. Every live session with this run's worker prefix takes a slot,
     including ones the database does not expect (orphans). The rest stay QUEUED,
     with their queue position recorded in state/;
  6. writes state/summary.json and jobs/summary.json, and hands off to a
     successor session when relaunch_after_s has passed.

Stages: QUEUED -> RUNNING -> PROMOTING -> DONE, or -> AWAITING_DECISION (operator: retry | skip).
With "review": true, a verified iteration goes to AWAITING_REVIEW instead; the reviewer's
decision is "pass" (-> PROMOTING -> DONE) or "restart" (-> QUEUED as iteration n+1, with the
decision's flag and refant files frozen in iter_<n+1>/inputs/). Attempts are numbered within
an iteration: tracks/<EB>/iter_<n>/attempt_<k>.json.

Retention ("retention" in the config; plan §3.2):
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

QA products ("qa" in the config; plan §5): every iteration's products, QA pages included, are
written straight to <products_dir>/<EB>/iter_<n>/ (default <root>/products), outside the track
database, so a reviewer's link never changes. COMPLETE marks the approved iteration
(<EB>/approved.json) instead of moving anything. Reviewers read them in a browser through the
ARC files service (url_base + the path after /arc). Each cycle the controller writes
<products_dir>/index.html (all tracks) and <products_dir>/<EB>/index.html (all iterations).
  products_dir   (default "products"; retention.products_dir is still read)
  group          CADC group given read access (POSIX ACL, shown as "Read" in Storage
                 Management) to each new QA folder; default none (owner and project group only)
  url_base       default https://ws-uv.canfar.net/arc/files
Tracks come from the config's "ebs" and from "request" actions in inbox/ (trackmanager.request).

Local run:    python -m trackmanager.controller --config project.json --root <run root>
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

from . import config as projconfig
from . import inbox, qa
from . import jobs as joblog
from .stages import calibrate, promote
from .backend import LIVE, make_backend
from .store import Root, age_s, append_jsonl, iso, log, read_json, write_json

REPO = Path(__file__).resolve().parent.parent
SETTLED = ("DONE", "SKIPPED", "AWAITING_DECISION", "AWAITING_REVIEW")


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

    # ---- database helpers ------------------------------------------------
    def event(self, eb, frm, to, reason):
        append_jsonl(self.r.events, {"ts": iso(), "actor": self.me, "eb": eb,
                                     "from": frm, "to": to, "reason": reason})

    def job_row(self, eb, n, k, att, **fields):
        ids = att.get("ids") or []
        append_jsonl(self.r.jobs, {"ts": iso(), "eb": eb, "iteration": n, "attempt": k, "kind": "calibrate",
                                   "name": att["name"], "job_id": ids[0] if ids else None,
                                   "log_dir": str(self.r.logs(eb, n, k)), **fields})

    def load_state(self, eb):
        st = read_json(self.r.state(eb))
        if st is None:
            st = {"eb": eb, "stage": "NEW", "iteration": 1, "attempt": 0, "hold": False}
            self.set_stage(st, "QUEUED", "discovered from config")
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

    def worker_limit(self):
        """The calibrate worker limit, re-read from project.yaml every cycle (a bad edit keeps the
        previous limits)."""
        return int(self.cfg.reload_limits().max_workers.get("calibrate", 0))

    def is_worker_name(self, name):
        return bool(name) and name.startswith(f"{self.prefix}{self.tag}-") \
            and not re.search(r"-ctrl-\d+$", name)

    def set_stage(self, st, to, reason):
        frm = st["stage"]
        st.update(stage=to, updated=iso(), detail=reason)
        if to == "QUEUED":
            st["queued_at"] = st["updated"]
        st.pop("queue_position", None)
        write_json(self.r.state(st["eb"]), st)
        self.event(st["eb"], frm, to, reason)
        log(f"{st['eb']}: {frm} -> {to} ({reason})")

    def session_name(self, eb, n, k):
        clean = re.sub(r"[^a-z0-9-]", "-", eb.lower()).strip("-")
        return f"{self.prefix}{self.tag}-{clean}-i{n}-a{k}"[:60]

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
        write_json(self.r.heartbeat, {"holder": self.me, "ts": iso(), "cycle": self.cycle_n,
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
        for e in self.cfg.track_specs():  # config tracks enter the queue before any requests
            self.load_state(e["eb"])
        inbox.apply(self)
        states = {eb: self.load_state(eb) for eb in self.track_ids()}
        for eb, st in states.items():
            if st["stage"] == "RUNNING":
                calibrate.reconcile(self, st)
            elif st["stage"] == "PROMOTING":  # a controller stopped part way through promotion
                promote.run(self, st)
        self.schedule(states)
        self.write_summaries(states)
        if not self.settled(states):
            self.maybe_handoff()

    def schedule(self, states):
        """Submit QUEUED tracks while worker slots are free; the rest wait in the database."""
        limit = self.worker_limit()
        running = [st for st in states.values() if st["stage"] == "RUNNING"]
        expected = set()
        for st in running:
            att = read_json(self.r.attempt(st["eb"], st.get("iteration", 1), st["attempt"])) or {}
            expected.add(att.get("name"))
        orphans = {s["id"]: s["name"] for s in self.sessions
                   if s["status"] in LIVE and self.is_worker_name(s.get("name"))
                   and s["name"] not in expected}
        for sid in set(orphans) - self.orphans:
            self.event("-", "workers", "orphan", f"live worker session {sid} ({orphans[sid]}) is not "
                       "a current attempt in the database; it takes a slot until it ends")
        self.orphans = set(orphans)
        in_use = len(running) + len(orphans)
        queue = sorted((st for st in states.values() if st["stage"] == "QUEUED" and not st.get("hold")),
                       key=lambda st: (-int(self.spec(st).get("priority", 0)),
                                       st.get("queued_at") or st.get("updated") or "", st["eb"]))
        waiting, submitted = [], 0
        for st in queue:
            if in_use < limit:
                calibrate.submit(self, st)
                in_use += 1
                submitted += 1
            else:
                waiting.append(st)
        for pos, st in enumerate(waiting, 1):
            if st.get("queue_position") != pos:
                st["queue_position"] = pos
                write_json(self.r.state(st["eb"]), st)
        if waiting:
            log(f"{len(waiting)} track(s) queued; {in_use}/{limit} worker slots in use")
        self.workers = {"limit": limit, "in_use": in_use, "running_tracks": len(running) + submitted,
                        "orphans": sorted(orphans), "queued": len(waiting),
                        "held": sum(st["stage"] == "QUEUED" and bool(st.get("hold")) for st in states.values())}

    @staticmethod
    def settled(states):
        return bool(states) and all(st["stage"] in SETTLED for st in states.values())

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
                                    "workers": self.workers,
                                    "qa_home_url": qa.qa_url(self, self.base_dir("products_dir") / "index.html"),
                                    "ebs": {eb: {k: st.get(k) for k in
                                                 ("stage", "iteration", "attempt", "detail", "updated", "hold",
                                                  "hold_reason", "queued_at", "queue_position", "qa_url")}
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
        settled = stages and stages <= set(SETTLED)
        if (a.max_cycles and ctl.cycle_n >= a.max_cycles) or (settled and not cfg.controller.keep_running):
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
