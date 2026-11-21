"""Placeholder worker: stands in for a real stage in tests and scheduler checks.

Standalone and stdlib-only, so it runs in any image with python3. It follows the
worker rules from plan §3.4:
  - exits 0 at once if a valid result.json already exists (skip if done);
  - takes worker.lock with O_EXCL, holding its session ID;
  - writes its log to /arc as it runs, so logs survive the ~1 h platform record;
  - writes run.json on exit and result.json last, through partial names.

Environment (set by the controller): TM_ROOT, EB, ITERATION, ATTEMPT, MODE, SLEEP_S.
Files: tracks/<EB>/raw/sdm.txt (staged once per track, like a real SDM),
tracks/<EB>/iter_<n>/{inputs/, logs/attempt_<k>/, work/, splits/, products/, output.txt, result.json};
worker.lock is per track. Like calibrate_worker.sh, it leaves an intermediate file in work/,
moves its "science splits" to splits/ and its QA products to products/, writes result.json,
then deletes work/ (TRIM_WORK=0 keeps it). result.json lists the inputs/ files it saw, with their sha256.
MODE: ok | fail | fail_first (fails on attempt 1 only) | no_result (exits 0 without
result.json) | oom (allocates memory until the container limit kills it).
"""
import datetime as dt
import hashlib
import json
import os
import resource
import socket
import sys
import time
from pathlib import Path


def iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def cgroup_limits():
    """CPU quota and memory limit the container actually got (cgroup v2, else v1)."""
    out = []
    for label, path in (("cpu.max", "/sys/fs/cgroup/cpu.max"),
                        ("memory.max", "/sys/fs/cgroup/memory.max"),
                        ("cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"),
                        ("mem_limit", "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            out.append(f"{label}={Path(path).read_text().strip()}")
        except OSError:
            pass
    return " ".join(out)


def write_atomic(path, data):
    tmp = path.with_name(f"partial-{path.name}")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def main():
    root = Path(os.environ["TM_ROOT"])
    eb = os.environ["EB"]
    iteration = int(os.environ.get("ITERATION", "1"))
    attempt = int(os.environ["ATTEMPT"])
    mode = os.environ.get("MODE", "ok")
    sleep_s = float(os.environ.get("SLEEP_S", "60"))
    session = os.environ.get("skaha_sessionid", "nosession")

    track = root / "tracks" / eb
    idir = track / f"iter_{iteration}"
    logdir = idir / "logs" / f"attempt_{attempt}"
    logdir.mkdir(parents=True, exist_ok=True)
    log = open(logdir / "stdout.log", "a", buffering=1)

    def say(msg):
        line = f"{iso()} [{session}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")

    say(f"start eb={eb} iteration={iteration} attempt={attempt} mode={mode} host={socket.gethostname()} "
        f"python={sys.version.split()[0]} OMP_NUM_THREADS={os.environ.get('OMP_NUM_THREADS')} "
        f"usable_cpus={len(os.sched_getaffinity(0))} {cgroup_limits()}")

    result = idir / "result.json"
    if result.exists():
        try:
            if json.loads(result.read_text()).get("ok"):
                say("valid result.json already exists; nothing to do")
                return 0
        except json.JSONDecodeError:
            say("result.json is malformed; keeping it for diagnosis and stopping")
            return 2

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

    started = time.time()
    run = {"eb": eb, "iteration": iteration, "attempt": attempt, "session": session, "mode": mode, "started": iso()}
    code = 1
    try:
        if mode == "oom":
            hog = []
            while True:
                hog.append(bytearray(256 * 1024 * 1024))
                say(f"allocated {len(hog) * 256} MiB")
                time.sleep(0.5)

        steps = max(1, int(sleep_s // 30))
        for i in range(steps):
            time.sleep(sleep_s / steps)
            say(f"working {i + 1}/{steps}")

        if mode == "fail" or (mode == "fail_first" and attempt == 1):
            say("simulated failure")
            code = 1
        elif mode == "no_result":
            say("exiting 0 without writing result.json")
            code = 0
        else:
            raw = track / "raw"
            if not (raw / "sdm.txt").exists():
                raw.mkdir(parents=True, exist_ok=True)
                (raw / "sdm.txt").write_text(f"placeholder raw data for {eb}\n")
            work = idir / "work"
            (work / f"{eb}.ms.split").mkdir(parents=True, exist_ok=True)
            (work / "intermediate.ms").write_text("full MS, caltables, ...\n")
            (work / f"{eb}.ms.split" / "table.dat").write_text(f"split of {eb}, iteration {iteration}\n")
            (idir / "splits").mkdir(exist_ok=True)
            os.replace(work / f"{eb}.ms.split", idir / "splits" / f"{eb}.ms.split")
            (idir / "products").mkdir(exist_ok=True)
            (idir / "products" / "index.html").write_text(f"QA for {eb}, iteration {iteration}\n")
            out = idir / "output.txt"
            tmp = out.with_name("partial-output.txt")
            tmp.write_text(f"placeholder product for {eb}, iteration {iteration}, attempt {attempt}\n")
            os.replace(tmp, out)
            inputs = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                      for f in sorted((idir / "inputs").glob("*")) if f.is_file()}
            write_atomic(result, {"ok": True, "eb": eb, "iteration": iteration, "attempt": attempt,
                                  "session": session, "inputs": inputs,
                                  "outputs": ["output.txt", f"splits/{eb}.ms.split", "products/index.html"],
                                  "finished": iso()})
            say("wrote output.txt and result.json")
            if os.environ.get("TRIM_WORK", "1") != "0":
                import shutil
                shutil.rmtree(work, ignore_errors=True)
                say("deleted work/ (intermediate files)")
            code = 0
    finally:
        run.update(exit_code=code, ended=iso(), wall_s=round(time.time() - started, 1),
                   peak_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1))
        write_atomic(logdir / "run.json", run)
        if lock.exists() and lock.read_text().strip() == session:
            lock.unlink()
        say(f"exit code {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
