"""What every worker stage shares: finding the session behind an attempt, saving the platform's
view of a finished one, and classifying a failure. `calibrate` and `stage_local` both build on this;
each supplies where its attempt file and logs live (an `Attempt`) and its own verify()."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..backend import LIVE, TERMINAL
from ..store import age_s, create_json, iso, log, read_json, write_json


@dataclass
class Attempt:
    """Where one attempt of a worker stage keeps its record."""
    kind: str            # calibrate | stage
    eb: str
    path: Path           # attempt_<k>.json
    logs: Path           # logs/attempt_<k>/
    iteration: int | None
    attempt: int
    stage: str           # the track's stage while it runs, for event text

    def job_row(self, ctl, att, **fields):
        extra = {"kind": self.kind, "log_dir": str(self.logs)}
        ctl.job_row(self.eb, self.iteration, self.attempt, att, **{**extra, **fields})


OOM_FRACTION = 0.7      # a killed worker whose last memory sample reached this share of its request ran out of memory
#                         (the real kill of 2026-11-22 was last sampled at 0.76: samples lag the final allocation)


def peak_anon(a: Attempt):
    """The worker's last sampled peak of anonymous memory, if it keeps one (calibrate_worker.sh does)."""
    try:
        return int((a.logs / "peak_anon_bytes").read_text().strip())
    except (OSError, ValueError):
        return None


def classify(status, run, platform_text, peak_anon_bytes=None, ram_gb=None):
    """failure_class for a failed attempt (plan §3.2 job database).

    An OOM kill leaves no text on CANFAR (checked 2026-11-22: the events show only start-up, the
    session log just stops, the status is plain Failed) and no run.json, because the kill is
    SIGKILL. So `oom` is inferred: no exit code was recorded, and the worker's own last memory
    sample reached OOM_FRACTION of what it asked for. The text match stays for platforms that do say it."""
    text = (platform_text or "").lower()
    if run and run.get("exit_code") == 0:
        return "no_output"
    if run and run.get("exit_code") not in (None, 0):
        return "failed"
    if "oomkilled" in text or "out of memory" in text:
        return "oom"
    if peak_anon_bytes and ram_gb and peak_anon_bytes >= OOM_FRACTION * ram_gb * 2**30:
        return "oom"
    if "deadline" in text:
        return "deadline"
    if status in TERMINAL:
        return "failed"
    return "lost"


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


def platform_text(a: Attempt) -> str:
    text = ""
    for f in ("events.txt", "session_info.json", "session.log"):
        p = a.logs / f
        text += p.read_text(errors="ignore") if p.exists() else ""
    return text


def reconcile(ctl, st, a: Attempt, verify):
    """Live -> wait. Terminal -> save the platform's view once, then verify. Missing -> verify
    from the outputs once the grace period has passed, since records vanish ~1 h after the end."""
    att = read_json(a.path)
    sess = None
    if att.get("ids"):
        sess = next((s for s in ctl.sessions if s["id"] == att["ids"][0]), None)
    if sess is None:
        cands = ctl.by_name(att["name"])
        sess = cands[0] if cands else None
        if sess and not att.get("ids"):
            att["ids"] = [sess["id"]]
            att["adopted"] = True
            write_json(a.path, att)
            ctl.event(a.eb, a.stage, a.stage, f"adopted session {sess['id']} by name "
                      "after an unconfirmed create()")
            a.job_row(ctl, att, status="adopted")
    status = sess["status"] if sess else None

    if status in LIVE:
        if status != att.get("last_status"):
            att["last_status"] = status
            att.setdefault("first_seen", {})[status] = iso()
            write_json(a.path, att)
            a.job_row(ctl, att, status=status)
        return
    if status in TERMINAL and not att.get("platform_saved"):
        save_platform(ctl, a.logs, att, sess["id"])
        att["platform_status"] = status
    if status is None:
        if att.get("last_status") is None and (age_s(att["requested_at"]) or 0) < ctl.cfg.controller.grace_s:
            return  # never listed yet: create() may be accepted but not visible
        att["platform_status"] = att.get("platform_status") or "missing"
    att["last_status"] = status or "missing"
    write_json(a.path, att)
    verify(ctl, st, att)


def create(a: Attempt, att) -> bool:
    """Write the attempt file before any API call (it is never overwritten). Returns False if it
    already existed: the caller reconciles that attempt instead of resubmitting."""
    try:
        create_json(a.path, att)
    except FileExistsError:
        log(f"{a.eb}: {a.path.parent.name}/{a.path.name} already exists; reconciling it instead of resubmitting")
        return False
    return True


def submit_session(ctl, a: Attempt, att, env):
    """Call backend.create for an attempt whose file is written; record the ids and a job row."""
    rq = att["request"]
    try:
        ids = ctl.backend.create(att["name"], rq["image"], rq["cmd"], rq["args"], env, rq["cores"], rq["ram"])
        if a.eb in ctl.lose_response:
            log(f"{a.eb}: simulating a lost create() response (discarding {ids})")
            ctl.lose_response.discard(a.eb)
            ids = []
    except Exception as e:
        att["create_error"] = repr(e)
        ids = []
    att["ids"] = ids or None
    att["created_at"] = iso()
    write_json(a.path, att)
    a.job_row(ctl, att, status="submitted" if ids else "submit_unconfirmed",
              cores=rq["cores"], ram=rq["ram"], image=rq["image"])
