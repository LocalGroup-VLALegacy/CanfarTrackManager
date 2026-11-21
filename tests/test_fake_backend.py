"""Fault-injection tests for the controller logic, using the local fake backend.

    python -m pytest -q tests   (from the repo root)

Each test runs the controller as a subprocess (as on CANFAR) in its own run root.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SKEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKEL))
from trackmanager.store import read_json, read_jsonl  # noqa: E402

BASE = {"name": "t", "poll_s": 1, "grace_s": 6, "auto_retry": 1, "max_running": 4,
        "relaunch_after_s": None,
        "worker": {"image": "local", "cmd": sys.executable, "cores": None, "ram": None},
        "controller": {"image": "local"}}


def setup(tmp_path, ebs, **over):
    cfg = {**BASE, **over, "ebs": ebs}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    return tmp_path / "cfg.json", tmp_path / "run"


def controller(cfg, root, *extra, env=None, wait=True, timeout=90):
    cmd = [sys.executable, "-m", "trackmanager.controller", "--config", str(cfg), "--root", str(root),
           "--backend", "fake", *extra]
    p = subprocess.Popen(cmd, cwd=SKEL, env={**os.environ, **(env or {})},
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         start_new_session=True)
    if wait:
        out, _ = p.communicate(timeout=timeout)
        p.output = out
    return p


def stages(root):
    return {eb: s["stage"] for eb, s in read_json(root / "state" / "summary.json")["ebs"].items()}


def submissions(root):
    return [r for r in read_jsonl(root / "jobs" / "jobs.jsonl")
            if r["status"] in ("submitted", "submit_unconfirmed")]


def test_happy_and_failure_paths(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "ok", "mode": "ok", "sleep_s": 1},
                                 {"eb": "flaky", "mode": "fail_first", "sleep_s": 1},
                                 {"eb": "bad", "mode": "fail", "sleep_s": 1}])
    controller(cfg, root)
    assert stages(root) == {"ok": "DONE", "flaky": "DONE", "bad": "AWAITING_DECISION"}
    att = read_json(root / "tracks" / "flaky" / "iter_1" / "attempt_1.json")
    assert att["outcome"] == "failed" and att["platform_saved"]


def test_kill_and_restart_does_not_resubmit(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "slow", "mode": "ok", "sleep_s": 12}])
    p = controller(cfg, root, wait=False)
    deadline = time.time() + 30
    while not (root / "tracks" / "slow" / "iter_1" / "attempt_1.json").exists() and time.time() < deadline:
        time.sleep(0.5)
    time.sleep(2)
    os.killpg(p.pid, signal.SIGKILL)  # crash: lease is not released
    p.wait()
    # A second controller is refused while the dead holder's heartbeat is fresh ...
    q = controller(cfg, root, timeout=30)
    assert q.returncode != 0 and "refusing to start a second controller" in q.output
    # ... and takes over once the heartbeat is stale (3 x poll_s).
    time.sleep(4)
    controller(cfg, root)
    assert stages(root) == {"slow": "DONE"}
    assert len(submissions(root)) == 1, "the restart must reconcile, not resubmit"


def test_lost_create_response_is_adopted_by_name(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "lost", "mode": "ok", "sleep_s": 3}])
    controller(cfg, root, "--lose-response", "lost")
    assert stages(root) == {"lost": "DONE"}
    att = read_json(root / "tracks" / "lost" / "iter_1" / "attempt_1.json")
    assert att.get("adopted") is True
    assert len(submissions(root)) == 1


def test_expired_record_is_verified_from_outputs(tmp_path):
    # Records vanish 1 s after the worker ends; the controller polls every 5 s.
    cfg, root = setup(tmp_path, [{"eb": "gone", "mode": "ok", "sleep_s": 2}], poll_s=5)
    controller(cfg, root, env={"TM_FAKE_RETENTION_S": "1"})
    assert stages(root) == {"gone": "DONE"}
    att = read_json(root / "tracks" / "gone" / "iter_1" / "attempt_1.json")
    assert att["platform_status"] == "missing" and not att.get("platform_saved")


def test_operator_retry_and_skip(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "a", "mode": "fail", "sleep_s": 1},
                                 {"eb": "b", "mode": "fail", "sleep_s": 1}])
    controller(cfg, root)
    assert stages(root) == {"a": "AWAITING_DECISION", "b": "AWAITING_DECISION"}
    for eb, action in (("a", "retry"), ("b", "skip")):
        subprocess.run([sys.executable, "-m", "trackmanager.decide", "--root", str(root), "--eb", eb,
                        "--action", action], cwd=SKEL, check=True, capture_output=True)
    controller(cfg, root)
    st = stages(root)
    assert st["b"] == "SKIPPED"
    assert st["a"] == "AWAITING_DECISION"  # retried once more (attempt 3) and failed again
    assert read_json(root / "state" / "a.json")["attempt"] == 3


def test_worker_skips_when_result_exists(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "x", "mode": "ok", "sleep_s": 1}])
    controller(cfg, root)
    env = {**os.environ, "TM_ROOT": str(root), "EB": "x", "ATTEMPT": "9", "MODE": "fail"}
    r = subprocess.run([sys.executable, str(SKEL / "trackmanager" / "worker.py")], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "nothing to do" in r.stdout


def lease_events(root):
    return [e for e in read_jsonl(root / "events.jsonl") if e["from"] == "lease"]


def test_handoff_to_successor(tmp_path):
    # Work outlasts relaunch_after_s, so a successor starts, takes the lease and finishes.
    cfg, root = setup(tmp_path, [{"eb": "long", "mode": "ok", "sleep_s": 12}], relaunch_after_s=2)
    controller(cfg, root)  # predecessor exits once the successor is waiting
    deadline = time.time() + 90
    while time.time() < deadline:
        if read_json(root / "state" / "long.json", {}).get("stage") == "DONE" \
                and read_json(root / "controller" / "lease.json", {}).get("released"):
            break
        time.sleep(1)
    to = [e["to"] for e in lease_events(root)]
    assert "handoff_requested" in to and to.count("held") >= 2
    assert stages(root) == {"long": "DONE"}
    assert len(submissions(root)) == 1


def test_pending_successor_is_cancelled_on_finish(tmp_path):
    # The successor is delayed 60 s, so the work finishes first and must cancel it.
    cfg, root = setup(tmp_path, [{"eb": "short", "mode": "ok", "sleep_s": 4}], relaunch_after_s=1,
                      successor_env={"TM_START_DELAY_S": 60})
    controller(cfg, root)
    lease = read_json(root / "controller" / "lease.json")
    assert lease["released"] and lease["handoff_to"] is None and lease["cancelled_successors"]
    assert "successor_cancelled" in [e["to"] for e in lease_events(root)]
    assert [e["to"] for e in lease_events(root)].count("held") == 1  # successor never took over
    recs = read_json(root / "fake_backend" / "sessions.json")
    assert recs[lease["cancelled_successors"][0]].get("destroyed") is True


def max_concurrent(root):
    """Replay events.jsonl: the most tracks in RUNNING at once, and the order they started."""
    running, peak, order = set(), 0, []
    for e in read_jsonl(root / "events.jsonl"):
        if e["to"] == "RUNNING" and e["from"] == "QUEUED":
            running.add(e["eb"])
            order.append(e["eb"])
        elif e["from"] == "RUNNING" and e["to"] != "RUNNING":
            running.discard(e["eb"])
        peak = max(peak, len(running))
    return peak, order


def test_worker_limit_holds_extra_tracks_in_queue(tmp_path):
    ebs = [{"eb": f"t{i}", "mode": "ok", "sleep_s": 2} for i in range(5)]
    cfg, root = setup(tmp_path, ebs, max_workers=2)
    p = controller(cfg, root, wait=False)
    deadline = time.time() + 30
    queued_seen = None
    while time.time() < deadline and queued_seen is None:  # catch the queue while it is waiting
        s = read_json(root / "state" / "summary.json", {})
        if (s.get("workers") or {}).get("queued"):
            queued_seen = s
        time.sleep(0.3)
    p.communicate(timeout=90)
    assert queued_seen and queued_seen["workers"]["limit"] == 2 and queued_seen["workers"]["in_use"] == 2
    positions = sorted(st["queue_position"] for st in queued_seen["ebs"].values()
                       if st["stage"] == "QUEUED")
    assert positions == list(range(1, queued_seen["workers"]["queued"] + 1))
    assert set(stages(root).values()) == {"DONE"}
    peak, order = max_concurrent(root)
    assert peak == 2 and order == [e["eb"] for e in ebs]  # never above the limit; FIFO order
    assert all("queue_position" not in read_json(root / "state" / f"{e['eb']}.json") for e in ebs)


def test_requested_track_waits_for_a_slot(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "cfg-a", "mode": "ok", "sleep_s": 2},
                                 {"eb": "cfg-b", "mode": "ok", "sleep_s": 2}], max_workers=1)
    req = lambda *a: subprocess.run([sys.executable, "-m", "trackmanager.request", "--root", str(root), *a],
                                    cwd=SKEL, capture_output=True, text=True)
    for args in (["--eb", "req-low"], ["--eb", "req-high", "--priority", "5", "--sleep-s", "1"],
                 ["--eb", "cfg-a"]):
        assert req(*args).returncode == 0
        time.sleep(1.1)  # distinct file stamps, so the inbox is read in this order
    assert req("--eb", "../bad").returncode != 0  # never writes outside inbox/
    (root / "inbox" / "decisions" / "zz-hand-written.json").write_text(
        json.dumps({"eb": "../bad", "action": "request", "by": "test"}))
    controller(cfg, root)
    assert stages(root) == {e: "DONE" for e in ("cfg-a", "cfg-b", "req-low", "req-high")}
    peak, order = max_concurrent(root)
    # Priority first, then the time a track was queued (config tracks are queued first).
    assert peak == 1 and order == ["req-high", "cfg-a", "cfg-b", "req-low"]
    st = read_json(root / "state" / "req-high.json")
    assert st["spec"]["priority"] == 5 and st["requested_by"]
    results = {d["eb"]: d["result"] for d in map(read_json, (root / "inbox" / "archive").glob("*.json"))}
    assert results["cfg-a"].startswith("ignored: already tracked")
    assert results["../bad"] == "ignored: invalid EB name"
    assert not (root / "state" / "..").joinpath("bad.json").exists()


def test_orphan_worker_session_takes_a_slot(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "real", "mode": "ok", "sleep_s": 1}], max_workers=1)
    sys.path.insert(0, str(SKEL))
    from trackmanager.backend import FakeBackend
    root.mkdir(parents=True)
    # A live session with this run's worker prefix that the database does not know about.
    stray = FakeBackend(root).create("tmrun-stray-a1", "local", "sleep", "5", {})[0]
    controller(cfg, root)
    assert stages(root) == {"real": "DONE"}
    ev = [e for e in read_jsonl(root / "events.jsonl") if e["to"] == "orphan"]
    assert ev and stray in ev[0]["reason"]
    stray_end = (root / "fake_backend" / stray / "exit_code").stat().st_mtime
    att = read_json(root / "tracks" / "real" / "iter_1" / "attempt_1.json")
    from trackmanager.store import parse_iso
    assert parse_iso(att["requested_at"]).timestamp() >= stray_end - 1  # waited for the slot


def decide(root, *args):
    r = subprocess.run([sys.executable, "-m", "trackmanager.decide", "--root", str(root), *args],
                       cwd=SKEL, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    time.sleep(1.1)  # distinct inbox file stamps


def test_review_restart_then_pass(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "trk", "mode": "ok", "sleep_s": 1}], review=True)
    controller(cfg, root)
    assert stages(root) == {"trk": "AWAITING_REVIEW"}
    flags = tmp_path / "flags.txt"
    flags.write_text("# Manual flagging for track trk\n# Config: D\nmode='manual' antenna='ea10' scan='5' reason='test'\n")
    decide(root, "--eb", "trk", "--action", "restart", "--flags", str(flags), "--refant-ignore", "ea26",
           "--notes", "RFI")
    decide(root, "--eb", "trk", "--action", "pass", "--iteration", "1")  # stale: applies after the restart
    controller(cfg, root)
    st = read_json(root / "state" / "trk.json")
    assert st["stage"] == "AWAITING_REVIEW" and st["iteration"] == 2 and st["attempt"] == 1
    i2 = root / "tracks" / "trk" / "iter_2"
    assert (i2 / "inputs" / "manual_flagging.txt").read_text() == flags.read_text()
    assert (i2 / "inputs" / "refant_ignore.txt").read_text() == "ea26\n"
    res = read_json(i2 / "result.json")
    assert res["iteration"] == 2 and set(res["inputs"]) == {"manual_flagging.txt", "refant_ignore.txt"}
    rev = read_json(root / "tracks" / "trk" / "iter_1" / "review.json")
    assert rev["verdict"] == "restart" and rev["notes"] == "RFI"
    archived = {d["action"]: d["result"] for d in map(read_json, (root / "inbox" / "archive").glob("*.json"))}
    assert archived["restart"] == "applied" and archived["pass"].startswith("ignored: decision for iteration 1")
    assert (root / "tracks" / "trk" / "iter_1" / "output.txt").exists()  # iteration 1 kept
    decide(root, "--eb", "trk", "--action", "pass")
    controller(cfg, root)
    assert stages(root) == {"trk": "DONE"}
    assert read_json(i2 / "review.json")["verdict"] == "pass"
    names = [r["name"] for r in submissions(root)]
    assert names == ["tmrun-trk-i1-a1", "tmrun-trk-i2-a1"]


def test_manual_review_and_help_hold_without_rerun(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "m", "mode": "ok", "sleep_s": 1}], review=True)
    controller(cfg, root)
    decide(root, "--eb", "m", "--action", "help_requested", "--notes", "odd bandpass")
    controller(cfg, root)
    st = read_json(root / "state" / "m.json")
    assert st["stage"] == "AWAITING_REVIEW" and st["hold"] and st["hold_reason"] == "help_requested"
    assert len(submissions(root)) == 1  # no rerun
    decide(root, "--eb", "m", "--action", "manual_review")
    decide(root, "--eb", "m", "--action", "pass")
    controller(cfg, root)
    st = read_json(root / "state" / "m.json")
    assert st["stage"] == "DONE" and not st["hold"]
    rev = read_json(root / "tracks" / "m" / "iter_1" / "review.json")
    assert [h["verdict"] for h in rev["history"]] == ["help_requested", "manual_review", "pass"]
