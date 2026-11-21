"""Session backends: the real CANFAR scheduler, and a local fake for logic tests."""
from __future__ import annotations

import datetime as dt
import os
import shlex
import signal
import subprocess
import uuid
from pathlib import Path

from .store import iso, now, parse_iso, read_json, write_json

LIVE = {"Pending", "Running", "Terminating"}
TERMINAL = {"Succeeded", "Completed", "Failed", "Error"}


class CanfarBackend:
    """Thin wrapper over canfar.sessions.Session (Python API; client 1.3.5 has no -o json)."""

    def __init__(self):
        from canfar.sessions import Session
        self._s = Session()

    def fetch(self) -> list[dict]:
        return self._s.fetch(kind="headless")

    def create(self, name, image, cmd, args, env, cores=None, ram=None) -> list[str]:
        return self._s.create(name=name, image=image, kind="headless", cmd=cmd, args=args,
                              env=env, cores=cores, ram=ram)

    def info(self, sid):
        return self._s.info([sid])

    def logs(self, sid):
        return (self._s.logs([sid]) or {}).get(sid)

    def events(self, sid):
        return self._s.events([sid])

    def destroy(self, sid) -> bool:
        return bool((self._s.destroy([sid]) or {}).get(sid))


class FakeBackend:
    """Runs each 'session' as a local subprocess and keeps skaha-like records.

    Records of finished sessions disappear after retention_s, as on CANFAR, so
    the controller's handling of missing records can be tested quickly.
    """

    def __init__(self, root, retention_s: float = 3600):
        self.db = Path(root) / "fake_backend" / "sessions.json"
        self.retention_s = retention_s

    def _load(self):
        return read_json(self.db, {})

    def _refresh(self, recs):
        for r in recs.values():
            if r["status"] == "Running":
                try:
                    os.waitpid(r["pid"], os.WNOHANG)
                except ChildProcessError:
                    pass
                code_file = Path(r["exit_file"])
                if code_file.exists():
                    code = int(code_file.read_text().strip() or 1)
                    r["status"] = "Succeeded" if code == 0 else "Failed"
                    r["endTime"] = iso(dt.datetime.fromtimestamp(code_file.stat().st_mtime,
                                                                 dt.timezone.utc))
        for sid in [s for s, r in recs.items() if r.get("endTime")
                    and (now() - parse_iso(r["endTime"])).total_seconds() > self.retention_s]:
            del recs[sid]
        write_json(self.db, recs)
        return recs

    def fetch(self):
        return [{k: r[k] for k in ("id", "name", "status", "startTime")}
                for r in self._refresh(self._load()).values()]

    def create(self, name, image, cmd, args, env, cores=None, ram=None):
        sid = uuid.uuid4().hex[:8]
        base = self.db.parent / sid
        base.mkdir(parents=True, exist_ok=True)
        exit_file = base / "exit_code"
        command = " ".join([shlex.quote(cmd), args, ";", "echo $? >", shlex.quote(str(exit_file))])
        proc = subprocess.Popen(["bash", "-c", command], start_new_session=True,
                                env={**os.environ, **{k: str(v) for k, v in env.items()},
                                     "skaha_sessionid": sid},
                                stdout=open(base / "session.log", "w"), stderr=subprocess.STDOUT)
        recs = self._load()
        recs[sid] = {"id": sid, "name": name, "status": "Running", "startTime": iso(),
                     "pid": proc.pid, "exit_file": str(exit_file), "image": image}
        write_json(self.db, recs)
        return [sid]

    def info(self, sid):
        return [self._load().get(sid, {})]

    def logs(self, sid):
        p = self.db.parent / sid / "session.log"
        return p.read_text() if p.exists() else None

    def events(self, sid):
        return [{sid: "fake backend: no platform events"}]

    def destroy(self, sid) -> bool:
        recs = self._load()
        r = recs.get(sid)
        if not r:
            return False
        try:
            os.killpg(r["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        if r["status"] == "Running":
            r.update(status="Failed", endTime=iso(), destroyed=True)
        write_json(self.db, recs)
        return True


def make_backend(kind: str, root):
    if kind == "canfar":
        return CanfarBackend()
    return FakeBackend(root, float(os.environ.get("TM_FAKE_RETENTION_S", 3600)))
