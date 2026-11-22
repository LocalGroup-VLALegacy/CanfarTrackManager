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

from helpers import make_project  # noqa: E402


def setup(tmp_path, ebs, **over):
    return make_project(tmp_path, ebs, **over)


def controller(cfg, root, *extra, env=None, wait=True, timeout=90):
    cmd = [sys.executable, "-m", "trackmanager.controller", "--project", str(cfg), "--root", str(root),
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
            if r.get("status") in ("submitted", "submit_unconfirmed") and r.get("kind") != "controller"]


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
    log = (root / "jobs" / "jobs.csv").read_text()
    assert "controller" in log and "tmrun-ctrl-" in log  # successors are in the project job log


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
    """Replay events.jsonl: the most tracks running a calibrate worker at once, and the order
    they started."""
    running, peak, order = set(), 0, []
    for e in read_jsonl(root / "events.jsonl"):
        if e.get("from_activity") == "queued" and e.get("activity") == "running":
            running.add(e["eb"])
            order.append(e["eb"])
        elif e.get("from_activity") == "running" and e.get("activity") != "running":
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
                       if st.get("activity") == "queued")
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
    i1 = root / "tracks" / "trk" / "iter_1"
    qa1 = root / "products" / "trk" / "iter_1"
    assert (i1 / "output.txt").exists() and (qa1 / "index.html").exists()  # record kept
    assert not (i1 / "splits").exists() and not (i1 / "work").exists()  # replaced by iteration 2
    assert (root / "tracks" / "trk" / "raw" / "sdm.txt").exists()  # raw stays until the track passes
    decide(root, "--eb", "trk", "--action", "pass")
    controller(cfg, root)
    assert stages(root) == {"trk": "DONE"}
    assert read_json(i2 / "review.json")["verdict"] == "pass"
    sci, prod = root / "science" / "trk", root / "products" / "trk" / "iter_2"
    assert (sci / "trk.ms.split" / "table.dat").read_text() == "split of trk, iteration 2\n"
    assert (prod / "index.html").read_text() == "QA for trk, iteration 2\n"  # never moved
    assert (prod / "inputs" / "manual_flagging.txt").read_text() == flags.read_text()
    assert read_json(prod.parent / "approved.json")["iteration"] == 2
    home = (root / "products" / "index.html").read_text()
    assert 'href="trk/iter_2/index.html"' in home and 'href="trk/index.html"' in home
    track_page = (root / "products" / "trk" / "index.html").read_text()
    assert "iteration 2</a> (approved)" in track_page and "iteration 1</a> (restart)" in track_page
    prov = read_json(sci / "provenance.json")
    assert prov["iteration"] == 2 and prov["splits"] == ["trk.ms.split"]
    assert set(prov["inputs_sha256"]) == {"manual_flagging.txt", "refant_ignore.txt"}
    assert not (root / "tracks" / "trk" / "raw").exists()  # deleted once the track passed
    assert not (i2 / "splits").exists()
    assert (qa1 / "index.html").exists()  # earlier iterations' QA stays, next to the approved one
    st = read_json(root / "state" / "trk.json")
    assert st["science_dir"] == str(sci) and st["completed_iteration"] == 2 and st["raw_deleted_at"]
    # Project job log: one row per job, with the verdict on its output and the promoted one marked.
    import csv
    jobs = {(r["iteration"], r["attempt"]): r for r in csv.DictReader(open(root / "jobs" / "jobs.csv"))}
    assert set(jobs) == {("1", "1"), ("2", "1")}
    assert jobs[("1", "1")]["verdict"] == "restart" and not jobs[("1", "1")]["promoted"]
    assert jobs[("2", "1")]["verdict"] == "pass" and jobs[("2", "1")]["promoted"] == st["completed_at"]
    assert all(r["outcome"] == "verified" and r["eb"] == "trk" and r["submitted"] for r in jobs.values())
    assert "tmrun-trk-i2-a1" not in (root / "jobs" / "jobs.log").read_text()  # session name is in csv
    tracks = list(csv.DictReader(open(root / "state" / "tracks.csv")))
    assert [(t["eb"], t["stage"], t["jobs"], t["completed_iteration"]) for t in tracks] == [("trk", "DONE", "2", "2")]
    r = subprocess.run([sys.executable, "-m", "trackmanager.jobs", "--root", str(root), "--status", "verified"],
                       cwd=SKEL, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.count("\ntrk") == 0 and len(r.stdout.splitlines()) == 3
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


def test_retention_trim_and_promotion_without_review(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "a", "mode": "ok", "sleep_s": 1},
                                 {"eb": "b", "mode": "ok", "sleep_s": 1, "env": {"TRIM_WORK": "0"}}],
                      retention={"science_dir": str(tmp_path / "calibrated" / "science"),
                                 "keep_raw_after_done": True},
                      qa={"products_dir": "qa_products"})
    controller(cfg, root)
    assert stages(root) == {"a": "DONE", "b": "DONE"}
    for eb in ("a", "b"):
        idir = root / "tracks" / eb / "iter_1"
        # b's worker kept work/ (TRIM_WORK=0); the controller trims it after verification.
        assert not (idir / "work").exists() and (idir / "logs" / "attempt_1" / "run.json").exists()
        assert (tmp_path / "calibrated" / "science" / eb / f"{eb}.ms.split").is_dir()  # absolute setting
        assert (root / "qa_products" / eb / "iter_1" / "index.html").exists()  # relative to the root
        assert (root / "tracks" / eb / "raw" / "sdm.txt").exists()  # keep_raw_after_done
    trimmed = [e for e in read_jsonl(root / "events.jsonl") if e["to"] == "trimmed"]
    assert [e["eb"] for e in trimmed] == ["b"]


def test_promotion_conflict_waits_for_operator_and_resumes(tmp_path):
    cfg, root = setup(tmp_path, [{"eb": "c", "mode": "ok", "sleep_s": 1}])
    clash = root / "science" / "c" / "c.ms.split"
    clash.mkdir(parents=True)  # an older copy the operator must look at
    controller(cfg, root)
    st = read_json(root / "state" / "c.json")
    assert st["stage"] == "AWAITING_DECISION" and "already exists" in st["detail"]
    assert (root / "tracks" / "c" / "iter_1" / "splits" / "c.ms.split").is_dir()  # nothing lost
    assert (root / "tracks" / "c" / "raw" / "sdm.txt").exists()
    # Operator moves the old copy away and puts the track back to PROMOTING (as the dashboard will).
    clash.rename(tmp_path / "old-c.ms.split")
    st["stage"] = "PROMOTING"
    (root / "state" / "c.json").write_text(json.dumps(st))
    controller(cfg, root)
    assert stages(root) == {"c": "DONE"}
    assert (root / "science" / "c" / "c.ms.split" / "table.dat").exists()
    assert not (root / "tracks" / "c" / "raw").exists()
    assert len(submissions(root)) == 1  # promotion never resubmits


def test_qa_links_and_group_read(tmp_path):
    import grp
    import shutil as sh
    from trackmanager import qa
    from trackmanager.controller import Controller
    group = grp.getgrgid(os.getgid()).gr_name
    from trackmanager import config as projconfig
    path, root = make_project(tmp_path, [], qa={"group": group})
    c = Controller(projconfig.load(path, check_paths=False), root, backend=None, me="t")
    assert qa.qa_url(c, "/arc/projects/X/calibrated/products/eb1/iter_2/index.html") == \
        "https://ws-uv.canfar.net/arc/files/projects/X/calibrated/products/eb1/iter_2/index.html"
    assert qa.qa_url(c, tmp_path / "index.html") is None  # not on /arc: no browser link
    assert qa.qa_dir(c, "eb1", 2) == tmp_path / "run" / "products" / "eb1" / "iter_2"
    if not sh.which("setfacl"):
        pytest.skip("no setfacl")
    qd = qa.qa_dir(c, "eb1", 2)
    (qd / "sub").mkdir(parents=True)
    (qd / "sub" / "p.html").write_text("x")
    assert qa.grant_qa_read(c, qd)
    (qd / "later.html").write_text("y")  # inherits the default entry
    for f in (qd, qd / "sub" / "p.html", qd / "later.html"):
        out = subprocess.run(["getfacl", "-p", str(f)], capture_output=True, text=True).stdout
        assert f"group:{group}:r" in out, out
