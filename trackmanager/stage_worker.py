"""stage_local worker (plan §3.4): put one track's raw SDM at tracks/<EB>/raw/<SDM> and verify it.

Standalone and stdlib-only (the session image only needs python3 and tar). The input is never
modified: an SDM directory is symlinked into raw/, an NRAO tar is unpacked into raw/.partial/ and
renamed. It follows the worker rules of §3.4: exit 0 at once if a valid manifest exists, take
worker.lock (O_EXCL, holding the session id), tee the log to /arc as it runs, write run.json on
exit and the manifest last, through a partial name.

Environment (set by the controller): TM_ROOT, EB, ATTEMPT, INPUT_PATH; optional EXPECT_SDM (the
archive's SDM name), EST_BYTES (TAP estimate), SIZE_TOLERANCE, MODE (ok | fail | fail_first |
no_result) and SLEEP_S, which the fake-backend tests use for fault injection.
Files: tracks/<EB>/stage/{manifest.json, logs/attempt_<k>/{stdout.log,run.json}}, tracks/<EB>/raw/.
manifest.json: ok, problems, eb, session, source, mode, input_path, sdm, sdm_path, bytes, main_rows,
bdf_files, est_bytes, ratio, fields{name: {scans, intents}}, finished. It is written even when a
check fails (ok false, problems listed), so the controller can show why.
Exit codes: 0 ok, 3 lock held, 10 input missing or unpack failed, 11 SDM invalid, 12 size outside
the tolerance, 1 anything else.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run by path, not -m
from trackmanager import sdm as sdmlib  # noqa: E402


def iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(f"partial-{path.name}")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def place(input_path: Path, raw: Path, expect: str | None, say):
    """Make raw/<SDM> from the input. Returns (sdm name, mode)."""
    if not input_path.exists():
        raise Fail(10, f"input {input_path} does not exist")
    if input_path.is_dir():
        name = expect or input_path.name
        if expect and input_path.name != expect:
            raise Fail(11, f"input directory is named {input_path.name}, the archive says {expect}")
        dest = raw / name
        if dest.is_symlink() or dest.exists():
            if dest.is_symlink() and os.path.realpath(dest) == os.path.realpath(input_path):
                say(f"{dest} already links to the input")
                return name, "symlink"
            if dest.is_symlink():
                dest.unlink()
            else:
                raise Fail(10, f"{dest} exists and is not a link to the input; not replacing it")
        tmp = raw / f".link-{name}"
        if tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(os.path.realpath(input_path))
        os.replace(tmp, dest)
        say(f"linked {dest} -> {os.path.realpath(input_path)}")
        return name, "symlink"
    part = raw / ".partial"
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir()
    say(f"unpacking {input_path}")
    r = subprocess.run(["tar", "-xf", str(input_path), "-C", str(part), "--strip-components=2"],
                       capture_output=True, text=True)
    if r.returncode:
        raise Fail(10, f"tar failed ({r.returncode}): {r.stderr.strip()[-300:]}")
    dirs = [p for p in part.iterdir() if p.is_dir()]
    if len(dirs) != 1:
        raise Fail(10, f"expected one SDM directory in the tar, found {[p.name for p in dirs]}")
    name = dirs[0].name
    if expect and name != expect:
        raise Fail(11, f"the tar holds {name}, the archive says {expect}")
    dest = raw / name
    if dest.exists():
        shutil.rmtree(dest)            # an earlier attempt's unverified unpack
    os.replace(dirs[0], dest)
    part.rmdir()
    return name, "tar"


def verify(sdm_path: Path, est_bytes, tol, man):
    problems = []
    for need in ("ASDM.xml", "Main.xml"):
        if not (sdm_path / need).is_file():
            problems.append(f"{need} is missing")
    if problems:
        return problems, 11
    try:
        man["main_rows"], man["bdf_files"] = sdmlib.main_rows(sdm_path), sdmlib.bdf_count(sdm_path)
        man["fields"] = sdmlib.field_intents(sdm_path)
    except sdmlib.SdmError as e:
        return [str(e)], 11
    if man["bdf_files"] != man["main_rows"]:
        problems.append(f"{man['bdf_files']} BDF files for {man['main_rows']} Main.xml rows")
    man["bytes"] = sdmlib.byte_size(sdm_path)
    if est_bytes:
        man["est_bytes"], man["ratio"] = est_bytes, round(man["bytes"] / est_bytes, 4)
        if abs(man["ratio"] - 1) > tol:
            problems.append(f"{man['bytes']} bytes is {man['ratio']:.3f}x the archive estimate "
                            f"(tolerance {tol:.0%})")
            return problems, 12 if len(problems) == 1 else 11
    return problems, 11 if problems else 0


def main():
    env = os.environ
    root, eb, attempt = Path(env["TM_ROOT"]), env["EB"], int(env["ATTEMPT"])
    session = env.get("skaha_sessionid", "nosession")
    mode = env.get("MODE", "ok")
    track = root / "tracks" / eb
    stage, raw = track / "stage", track / "raw"
    logdir = stage / "logs" / f"attempt_{attempt}"
    logdir.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    logf = open(logdir / "stdout.log", "a", buffering=1)

    def say(msg):
        line = f"{iso()} [{session}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")

    say(f"start eb={eb} attempt={attempt} input={env.get('INPUT_PATH')} expect={env.get('EXPECT_SDM')}")
    manifest = stage / "manifest.json"
    try:
        old = json.loads(manifest.read_text())
        if old.get("ok") and Path(old["sdm_path"]).exists():
            say("a valid manifest.json already exists; nothing to do")
            return 0
    except (OSError, ValueError, KeyError):
        pass

    lock = track / "worker.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
        os.write(fd, session.encode())
        os.close(fd)
    except FileExistsError:
        holder = lock.read_text().strip()
        if holder != session:
            say(f"worker.lock is held by {holder}; refusing to run a second worker")
            return 3

    started, code = time.time(), 1
    run = {"eb": eb, "kind": "stage", "attempt": attempt, "session": session, "mode": mode, "started": iso()}
    try:
        time.sleep(float(env.get("SLEEP_S", "0")))
        if mode == "fail" or (mode == "fail_first" and attempt == 1):
            say("simulated failure")
            return 1
        if mode == "no_result":
            say("exiting 0 without writing manifest.json")
            code = 0
            return 0
        input_path = Path(env.get("INPUT_PATH", ""))
        man = {"ok": False, "problems": [], "eb": eb, "session": session, "input_path": str(input_path)}
        try:
            if not env.get("INPUT_PATH"):
                raise Fail(10, "INPUT_PATH is not set")
            name, how = place(input_path, raw, env.get("EXPECT_SDM") or None, say)
            sdm_path = raw / name
            man.update(sdm=name, sdm_path=str(sdm_path), mode=how,
                       source="tar" if how == "tar" else "directory")
            problems, code = verify(sdm_path, int(float(env.get("EST_BYTES") or 0)),
                                    float(env.get("SIZE_TOLERANCE") or 0.05), man)
        except Fail as f:
            problems, code = [str(f)], f.code
        man.update(ok=not problems, problems=problems, finished=iso())
        write_atomic(manifest, man)
        say("manifest.json written: " + ("ok" if not problems else "; ".join(problems)))
        run.update(sdm_bytes=man.get("bytes"))
        return code
    finally:
        run.update(exit_code=code, ended=iso(), wall_s=round(time.time() - started, 1),
                   peak_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1))
        write_atomic(logdir / "run.json", run)
        if lock.exists() and lock.read_text().strip() == session:
            lock.unlink()
        say(f"exit code {code}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:      # unexpected: still leave the reason in the session log
        print(f"{iso()} stage_worker crashed: {e!r}", flush=True)
        sys.exit(1)
