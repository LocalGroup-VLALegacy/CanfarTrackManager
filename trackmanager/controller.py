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

Stages: QUEUED -> RUNNING -> DONE, or -> AWAITING_DECISION (operator: retry | skip).
With "review": true, a verified iteration goes to AWAITING_REVIEW instead of DONE; the
reviewer's decision is "pass" (-> DONE) or "restart" (-> QUEUED as iteration n+1, with the
decision's flag and refant files frozen in iter_<n+1>/inputs/). Attempts are numbered within
an iteration: tracks/<EB>/iter_<n>/attempt_<k>.json.
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
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .backend import LIVE, TERMINAL, make_backend
from .store import (Root, age_s, append_jsonl, create_json, iso, now, parse_iso,
                    read_json, read_jsonl, write_json)

REPO = Path(__file__).resolve().parent.parent
SETTLED = ("DONE", "SKIPPED", "AWAITING_DECISION", "AWAITING_REVIEW")
INPUT_FILES = ("manual_flagging.txt", "refant_ignore.txt")  # what a restart may carry
# Reviewer verdicts (flag-tab M1): COMPLETE -> pass, RESTART -> restart; MANUAL REVIEW and
# HELP REQUESTED hold the track for the operator (no rerun) until a later pass or restart.
REVIEW_ACTIONS = ("pass", "restart", "manual_review", "help_requested")


def log(msg):
    print(f"{iso()} {msg}", flush=True)


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


class Controller:
    def __init__(self, cfg: dict, root: Path, backend, me: str, lose_response=(),
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
        self.prefix = re.sub(r"[^a-z0-9]", "", str(cfg.get("session_prefix", "tm")).lower()) or "tm"
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
        append_jsonl(self.r.jobs, {"ts": iso(), "eb": eb, "iteration": n, "attempt": k,
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
        ebs = [e["eb"] for e in self.cfg["ebs"]]
        sdir = self.r.summary.parent
        extra = sorted(p.stem for p in sdir.glob("*.json") if p.name != "summary.json") \
            if sdir.exists() else []
        return ebs + [eb for eb in extra if eb not in ebs]

    def spec(self, st):
        return next((e for e in self.cfg["ebs"] if e["eb"] == st["eb"]), None) or st.get("spec") or {}

    def worker_limit(self):
        """max_workers (or legacy max_running), re-read from the config file every cycle."""
        cfg = self.cfg
        if self.config_path:
            try:
                cfg = {**cfg, **{k: v for k, v in json.loads(Path(self.config_path).read_text()).items()
                                 if k in ("max_workers", "max_running")}}
            except (OSError, ValueError) as e:
                log(f"config re-read failed, keeping the previous limit: {e!r}")
        limit = cfg.get("max_workers", cfg.get("max_running", 1))
        self.cfg["max_workers"] = limit
        return int(limit)

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
        return a is None or a > 3 * self.cfg["poll_s"]

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
        for e in self.cfg["ebs"]:  # config tracks enter the queue before any requests
            self.load_state(e["eb"])
        self.apply_inbox()
        states = {eb: self.load_state(eb) for eb in self.track_ids()}
        for eb, st in states.items():
            if st["stage"] == "RUNNING":
                self.reconcile(st)
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
                self.submit(st)
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
    def submit(self, st):
        eb = st["eb"]
        spec = self.spec(st)
        n, k = st.get("iteration", 1), st["attempt"] + 1
        lock = self.r.track(eb) / "worker.lock"
        lock.unlink(missing_ok=True)  # every earlier attempt is terminal by now
        self.r.inputs(eb, n).mkdir(parents=True, exist_ok=True)
        if k == 1 and n > 1:
            self.drop_previous_work(eb, n - 1)
        w = self.cfg["worker"]
        env = {"TM_ROOT": str(self.r.path), "EB": eb, "ITERATION": str(n), "ATTEMPT": str(k),
               "MODE": spec.get("mode", "ok"), "SLEEP_S": str(spec.get("sleep_s", 60))}
        # Real workers (e.g. calibrate_worker.sh) take their inputs from config env. The CASA
        # install is a project setting ("casa_home"); worker.env or an EB's env may override it.
        if self.cfg.get("casa_home"):
            env["CASA_HOME"] = str(self.cfg["casa_home"])
        env.update({k: str(v) for k, v in {**w.get("env", {}), **spec.get("env", {})}.items()})
        if w.get("cores"):
            env["OMP_NUM_THREADS"] = str(w["cores"])
        att = {"name": self.session_name(eb, n, k), "iteration": n, "attempt": k, "requested_at": iso(),
               "request": {"image": w["image"], "cmd": w["cmd"],
                           "args": w.get("args", f"{REPO}/trackmanager/worker.py"), "env": env,
                           "cores": w.get("cores"), "ram": w.get("ram")},
               "ids": None, "last_status": None, "platform_saved": False}
        try:
            create_json(self.r.attempt(eb, n, k), att)  # attempt file before any API call
        except FileExistsError:
            log(f"{eb}: iter_{n}/attempt_{k}.json already exists; reconciling it instead of resubmitting")
            st["attempt"] = k
            self.set_stage(st, "RUNNING", f"found existing iteration {n} attempt {k}")
            return
        st["attempt"] = k
        self.set_stage(st, "RUNNING", f"submitting iteration {n} attempt {k}")
        rq = att["request"]
        try:
            ids = self.backend.create(att["name"], rq["image"], rq["cmd"], rq["args"], env,
                                      rq["cores"], rq["ram"])
            if eb in self.lose_response:
                log(f"{eb}: simulating a lost create() response (discarding {ids})")
                self.lose_response.discard(eb)
                ids = []
        except Exception as e:
            att["create_error"] = repr(e)
            ids = []
        att["ids"] = ids or None
        att["created_at"] = iso()
        write_json(self.r.attempt(eb, n, k), att)
        self.job_row(eb, n, k, att, status="submitted" if ids else "submit_unconfirmed")

    def drop_previous_work(self, eb, prev):
        """retention.drop_prev_iter_work: delete iter_<prev>/work once its products are kept."""
        if not (self.cfg.get("retention") or {}).get("drop_prev_iter_work"):
            return
        work, res = self.r.iter_dir(eb, prev) / "work", read_json(self.r.result(eb, prev))
        if work.exists() and res and res.get("ok") and (self.r.iter_dir(eb, prev) / "products").is_dir():
            shutil.rmtree(work, ignore_errors=True)
            self.event(eb, "retention", "dropped", f"iter_{prev}/work deleted (products kept)")

    # ---- monitoring and verification ------------------------------------
    def reconcile(self, st):
        eb, n, k = st["eb"], st.get("iteration", 1), st["attempt"]
        att = read_json(self.r.attempt(eb, n, k))
        sess = None
        if att.get("ids"):
            sess = next((s for s in self.sessions if s["id"] == att["ids"][0]), None)
        if sess is None:
            cands = self.by_name(att["name"])
            sess = cands[0] if cands else None
            if sess and not att.get("ids"):
                att["ids"] = [sess["id"]]
                att["adopted"] = True
                write_json(self.r.attempt(eb, n, k), att)
                self.event(eb, "RUNNING", "RUNNING", f"adopted session {sess['id']} by name "
                           "after an unconfirmed create()")
                self.job_row(eb, n, k, att, status="adopted")
        status = sess["status"] if sess else None

        if status in LIVE:
            if status != att.get("last_status"):
                att["last_status"] = status
                att.setdefault("first_seen", {})[status] = iso()
                write_json(self.r.attempt(eb, n, k), att)
                self.job_row(eb, n, k, att, status=status)
            return
        if status in TERMINAL and not att.get("platform_saved"):
            self.save_platform(self.r.logs(eb, n, k), att, sess["id"])
            att["platform_status"] = status
        if status is None:
            if att.get("last_status") is None and (age_s(att["requested_at"]) or 0) < self.cfg["grace_s"]:
                return  # never listed yet: create() may be accepted but not visible
            att["platform_status"] = att.get("platform_status") or "missing"
        att["last_status"] = status or "missing"
        write_json(self.r.attempt(eb, n, k), att)
        self.verify(st, att)

    def save_platform(self, d, att, sid):
        d.mkdir(parents=True, exist_ok=True)
        errors = {}
        for name, fn, fname in (("info", self.backend.info, "session_info.json"),
                                ("events", self.backend.events, "events.txt"),
                                ("logs", self.backend.logs, "session.log")):
            try:
                out = fn(sid)
                text = out if isinstance(out, str) else json.dumps(out, indent=1, default=str)
                (d / fname).write_text(text or "")
            except Exception as e:
                errors[name] = repr(e)
        att["platform_saved"] = iso()
        if errors:
            att["platform_errors"] = errors

    def verify(self, st, att):
        eb, n, k = st["eb"], st.get("iteration", 1), st["attempt"]
        idir = self.r.iter_dir(eb, n)
        res = read_json(self.r.result(eb, n))
        run = read_json(self.r.logs(eb, n, k) / "run.json") or {}
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
            write_json(self.r.attempt(eb, n, k), att)
            self.job_row(eb, n, k, att, status="verified", failure_class="none", **common)
            to = "AWAITING_REVIEW" if self.cfg.get("review") else "DONE"
            self.set_stage(st, to, f"iteration {n} attempt {k} verified "
                                   f"(platform: {common['platform_status']})")
            return
        text = ""
        for f in ("events.txt", "session_info.json", "session.log"):
            p = self.r.logs(eb, n, k) / f
            text += p.read_text(errors="ignore") if p.exists() else ""
        fclass = classify(att.get("platform_status"), run, text)
        att["outcome"] = fclass
        write_json(self.r.attempt(eb, n, k), att)
        self.job_row(eb, n, k, att, status="failed_verification", failure_class=fclass, **common)
        live_same_name = any(s["status"] in LIVE for s in self.by_name(att["name"]))
        if k <= self.cfg["auto_retry"] and not live_same_name:
            self.set_stage(st, "QUEUED", f"iteration {n} attempt {k} {fclass}; automatic retry")
        else:
            self.set_stage(st, "AWAITING_DECISION",
                           f"iteration {n} attempt {k} {fclass}; operator decision needed")

    # ---- operator input --------------------------------------------------
    def apply_inbox(self):
        for f in sorted(self.r.inbox.glob("*.json")) if self.r.inbox.exists() else []:
            d = read_json(f, {})
            eb, action = d.get("eb"), d.get("action")
            valid = bool(eb) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", eb) is not None
            st = read_json(self.r.state(eb)) if valid else None
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
                    self.set_stage(st, "QUEUED", f"requested by {d.get('by', '?')}")
                    result = "applied"
            elif st and action in REVIEW_ACTIONS:
                result = self.apply_review(st, d)
            elif st and action == "retry" and st["stage"] == "AWAITING_DECISION":
                self.set_stage(st, "QUEUED", f"operator retry ({d.get('by', '?')})")
                result = "applied"
            elif st and action == "skip" and st["stage"] == "AWAITING_DECISION":
                self.set_stage(st, "SKIPPED", f"operator skip ({d.get('by', '?')})")
                result = "applied"
            elif st and action in ("hold", "release"):
                st["hold"] = action == "hold"
                write_json(self.r.state(eb), st)
                self.event(eb, st["stage"], st["stage"], f"operator {action}")
                result = "applied"
            self.r.inbox_archive.mkdir(parents=True, exist_ok=True)
            write_json(self.r.inbox_archive / f.name, {**d, "result": result, "processed": iso()})
            f.unlink()
            log(f"inbox {f.name}: {action} for {eb} -> {result}")

    def apply_review(self, st, d):
        """A reviewer verdict for the current iteration: pass -> DONE, restart -> iteration n+1,
        manual_review / help_requested -> HOLD flag set (stage stays AWAITING_REVIEW), shown to the
        operator; a later pass or restart for the same iteration clears that hold."""
        eb, n, by = st["eb"], st.get("iteration", 1), d.get("by", "?")
        if d.get("iteration") is not None and int(d["iteration"]) != n:
            return f"ignored: decision for iteration {d['iteration']}, track is at iteration {n}"
        if st["stage"] != "AWAITING_REVIEW":
            return f"ignored: track is {st['stage']}, not AWAITING_REVIEW"
        inputs = d.get("inputs") or {}
        bad = sorted(set(inputs) - set(INPUT_FILES))
        if bad:
            return f"ignored: unknown input files {bad} (allowed: {list(INPUT_FILES)})"
        review = {"iteration": n, "verdict": d["action"], "by": by, "decided": d.get("ts"),
                  "applied": iso(), "notes": d.get("notes", ""), "inputs_next": sorted(inputs)}
        if d["action"] in ("manual_review", "help_requested"):
            history = (read_json(self.r.review(eb, n)) or {}).get("history", [])
            write_json(self.r.review(eb, n), {**review, "history": history + [review]})
            st.update(hold=True, hold_reason=d["action"])
            write_json(self.r.state(eb), st)
            self.event(eb, st["stage"], st["stage"], f"{d['action']} from {by}: held for the operator"
                       + (f" ({d['notes']})" if d.get("notes") else ""))
            return "applied"
        history = (read_json(self.r.review(eb, n)) or {}).get("history", [])
        write_json(self.r.review(eb, n), {**review, "history": history + [review]})
        if st.get("hold_reason") in ("manual_review", "help_requested"):
            st.update(hold=False, hold_reason=None)  # the reviewer has now decided
        if d["action"] == "pass":
            self.set_stage(st, "DONE", f"iteration {n} passed review ({by})")
            return "applied"
        nxt = self.r.inputs(eb, n + 1)
        nxt.mkdir(parents=True, exist_ok=True)
        for name, text in inputs.items():  # frozen now; later sheet edits don't change this run
            tmp = nxt / f"partial-{name}"
            tmp.write_text(text)
            os.replace(tmp, nxt / name)
        st.update(iteration=n + 1, attempt=0)
        self.set_stage(st, "QUEUED", f"restart requested by {by}: iteration {n + 1} "
                                     f"with {', '.join(sorted(inputs)) or 'no new inputs'}")
        return "applied"

    # ---- summaries and handoff ------------------------------------------
    def write_summaries(self, states):
        write_json(self.r.summary, {"ts": iso(), "controller": self.me, "cycle": self.cycle_n,
                                    "workers": self.workers,
                                    "ebs": {eb: {k: st.get(k) for k in
                                                 ("stage", "iteration", "attempt", "detail", "updated", "hold",
                                                  "hold_reason", "queued_at", "queue_position")}
                                            for eb, st in states.items()}})
        latest = {}
        for row in read_jsonl(self.r.jobs):
            key = f"{row['eb']}#i{row.get('iteration', 1)}a{row['attempt']}"
            latest[key] = {**latest.get(key, {}), **{k: v for k, v in row.items() if v is not None}}
        write_json(self.r.jobs_summary, latest)

    def maybe_handoff(self):
        after = self.cfg.get("relaunch_after_s")
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
                    and (age_s(lease.get("handoff_at")) or 0) > self.cfg["grace_s"]:
                self.event("-", "lease", "handoff_failed", f"successor {succ} not live; will retry")
                lease["handoff_to"] = None
                write_json(self.r.lease, lease)
            return
        if (age_s(self.started) or 0) < after:
            return
        c = self.cfg["controller"]
        name = f"{self.prefix}{self.tag}-ctrl-{self.cycle_n}"
        args = f"{REPO}/scripts/run_controller.sh {self.config_path} {self.r.path}"
        try:
            env = {"TM_HANDOFF_FROM": self.me, "TM_BACKEND": self.backend_kind,
                   **{k: str(v) for k, v in self.cfg.get("successor_env", {}).items()}}
            if self.backend_kind == "fake":
                env["TM_PYTHON"] = sys.executable
            ids = self.backend.create(name, c["image"], "bash", args, env, None, None)
        except Exception as e:
            log(f"successor create failed: {e!r}")
            return
        if ids:
            lease.update(handoff_to=ids[0], handoff_at=iso())
            write_json(self.r.lease, lease)
            self.event("-", "lease", "handoff_requested", f"successor session {ids[0]} ({name})")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--root", required=True, help="project root for this run (created if needed)")
    p.add_argument("--backend", choices=["canfar", "fake"], default="canfar")
    p.add_argument("--max-cycles", type=int, default=0, help="stop after N cycles (0 = until all settle)")
    p.add_argument("--lose-response", default="", help="comma-separated EBs whose create() response is discarded")
    a = p.parse_args(argv)

    cfg = json.loads(Path(a.config).read_text())
    casa_home = cfg.get("casa_home")
    if casa_home and not os.access(Path(casa_home) / "bin" / "casa", os.X_OK):
        sys.exit(f"casa_home {casa_home} has no executable bin/casa; fix the config")
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    sid = os.environ.get("skaha_sessionid", "nosession")
    me = sid if os.environ.get("TM_HEADLESS") else f"{sid}-local{os.getpid()}"
    backend = make_backend(a.backend, root)
    ctl = Controller(cfg, root, backend, me, [e for e in a.lose_response.split(",") if e],
                     str(Path(a.config).resolve()), a.backend)
    ctl.acquire_lease()
    ctl.event("-", "controller", "start", json.dumps(
        {"host": socket.gethostname(), "python": platform.python_version(), "backend": a.backend,
         "handoff_from": os.environ.get("TM_HANDOFF_FROM"), **cert_info()}))
    while True:
        ctl.cycle()
        stages = {st["stage"] for st in (read_json(ctl.r.summary) or {}).get("ebs", {}).values()}
        settled = stages and stages <= set(SETTLED)
        if (a.max_cycles and ctl.cycle_n >= a.max_cycles) or (settled and not cfg.get("keep_running")):
            log(f"stopping after cycle {ctl.cycle_n}: stages {sorted(stages)}")
            if cfg.get("cancel_successor_on_finish", True):
                ctl.cancel_successor("work finished")
            lease = read_json(ctl.r.lease)
            lease.update(released=True, released_at=iso())
            write_json(ctl.r.lease, lease)
            ctl.event("-", "lease", "released", "controller finished normally")
            break
        time.sleep(cfg["poll_s"])


if __name__ == "__main__":
    main()
