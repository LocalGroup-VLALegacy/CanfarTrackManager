"""STAGING (M3): stage_local under the controller, on the fake backend.

Each test runs the controller as a subprocess, as in test_fake_backend.py. Tracks name an input
(`path`), so they start at STAGING/queued; the placeholder calibrate worker takes over after
staging verifies."""
import os
import signal
import subprocess
import sys
import time

import pytest

from helpers import FIXTURES, REPO, make_fake_sdm, make_fake_tar, make_project
from test_fake_backend import controller, stages, submissions
from test_stages import decide, events
from trackmanager import states as S
from trackmanager.controller import Controller
from trackmanager.store import read_json, read_jsonl

PIPELINE_REPO = REPO.parent / "ReductionPipeline"
needs_pipeline = pytest.mark.skipif(not (PIPELINE_REPO / "lband_pipeline" / "read_config_files.py").exists(),
                                    reason="needs the ReductionPipeline checkout next to this repo")


def project(tmp_path, tracks, **over):
    return make_project(tmp_path, tracks, **over)


def state(root, eb):
    return read_json(root / "state" / f"{eb}.json")


def stage_jobs(root):
    return [r for r in read_jsonl(root / "jobs" / "jobs.jsonl") if r.get("kind") == "stage"]


def stage_submissions(root):
    return [r for r in submissions(root) if r.get("kind") == "stage"]


def sdm_in(tmp_path, name="in", **kw):
    (tmp_path / name).mkdir(exist_ok=True)
    return make_fake_sdm(tmp_path / name, **kw)


def test_directory_input_is_staged_then_calibrated_to_done(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [{"eb": "a", "path": str(sdm), "mode": "ok", "sleep_s": 1}])
    controller(cfg, root)
    assert stages(root) == {"a": "DONE"}
    man = read_json(root / "tracks" / "a" / "stage" / "manifest.json")
    assert man["ok"] and man["mode"] == "symlink" and man["sdm"] == sdm.name
    st = state(root, "a")
    assert st["sdm"] == sdm.name and st["sdm_bytes"] == man["bytes"] and st["stage_attempt"] == 1
    # staging and calibration are separate jobs with separate keys
    summary = read_json(root / "jobs" / "summary.json")
    assert summary["a#stage-a1"]["kind"] == "stage" and summary["a#stage-a1"]["outcome"] == "verified"
    assert summary["a#i1a1"]["kind"] == "calibrate"
    path = [(e["from"], e["to"]) for e in events(root) if e["eb"] == "a" and e["from"] != e["to"]]
    assert path[:2] == [("NEW", "STAGING"), ("STAGING", "CALIBRATING")]


def test_tar_input_is_unpacked_and_the_tar_is_left_alone(tmp_path):
    src = sdm_in(tmp_path, "src")
    tar = make_fake_tar(tmp_path / "x.tar", src)
    before = tar.read_bytes()
    cfg, root = project(tmp_path, [{"eb": "t", "path": str(tar), "mode": "ok", "sleep_s": 1}], review=True)
    controller(cfg, root)
    assert stages(root) == {"t": "AWAITING_REVIEW"}
    raw = root / "tracks" / "t" / "raw" / src.name
    assert (raw / "Main.xml").is_file() and not raw.is_symlink()
    assert tar.read_bytes() == before


def test_missing_input_stops_before_any_session_and_retry_works_once_it_exists(tmp_path):
    later = tmp_path / "in" / "later"
    cfg, root = project(tmp_path, [{"eb": "m", "path": str(later), "mode": "ok", "sleep_s": 1}])
    controller(cfg, root)
    st = state(root, "m")
    assert (st["stage"], st["reason"], st["failed_from"]) == ("AWAITING_DECISION", "failed", "STAGING")
    assert "does not exist" in st["detail"] and not submissions(root)
    (tmp_path / "in").mkdir(exist_ok=True)
    make_fake_sdm(tmp_path / "in", name="later")
    decide(root, "m", "retry")
    controller(cfg, root)
    assert stages(root) == {"m": "DONE"} and len(stage_submissions(root)) == 1


def test_bad_sdm_goes_to_the_operator_without_an_automatic_retry(tmp_path):
    sdm = sdm_in(tmp_path, bdf_files=1)           # 3 Main.xml rows, 1 BDF
    cfg, root = project(tmp_path, [{"eb": "bad", "path": str(sdm), "mode": "ok", "sleep_s": 1}], auto_retry=2)
    controller(cfg, root)
    st = state(root, "bad")
    assert (st["stage"], st["failed_from"]) == ("AWAITING_DECISION", "STAGING")
    assert "BDF files" in st["detail"] and len(stage_submissions(root)) == 1   # a verdict, not a flake
    # the operator repairs the data and retries: STAGING again, then on to DONE
    (sdm / "ASDMBinary" / "uid____evla_bdf_a").write_bytes(b"0")
    (sdm / "ASDMBinary" / "uid____evla_bdf_b").write_bytes(b"0")
    decide(root, "bad", "retry")
    controller(cfg, root)
    assert stages(root) == {"bad": "DONE"}
    assert len(stage_submissions(root)) == 2 and state(root, "bad")["stage_attempt"] == 2


def test_a_worker_that_leaves_no_manifest_is_retried_once_then_goes_to_the_operator(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [{"eb": "flaky", "path": str(sdm), "stage_mode": "fail_first", "mode": "ok",
                                    "sleep_s": 1},
                                   {"eb": "dead", "path": str(sdm), "stage_mode": "fail", "mode": "ok",
                                    "sleep_s": 1}])
    controller(cfg, root)
    assert stages(root) == {"flaky": "DONE", "dead": "AWAITING_DECISION"}
    assert read_json(root / "tracks" / "flaky" / "stage" / "attempt_1.json")["outcome"] == "failed"
    assert state(root, "dead")["failure_class"] == "failed"
    assert [r["eb"] for r in stage_submissions(root)].count("dead") == 2      # auto_retry = 1 default


def test_stage_slots_are_limited_separately_from_calibrate_slots(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [{"eb": f"s{i}", "path": str(sdm), "stage_sleep_s": 3, "mode": "ok",
                                    "sleep_s": 1} for i in range(3)], max_workers_stage=1, max_workers=3)
    controller(cfg, root)
    assert set(stages(root).values()) == {"DONE"}
    live = peak = 0
    for e in events(root):
        if e.get("activity") == "running" and e.get("from_activity") == "queued" and e["to"] == "STAGING":
            live += 1
        elif e["from"] == "STAGING" and e["to"] != "STAGING":
            live -= 1
        peak = max(peak, live)
    assert peak == 1


def test_orphan_stage_sessions_take_a_stage_slot_not_a_calibrate_slot():
    name = Controller.session_name
    c = type("C", (), {"prefix": "tm", "tag": "run"})()
    assert name(c, "EB-1", None, 2) == "tmrun-eb-1-stage-a2"
    assert name(c, "EB-1", 3, 1) == "tmrun-eb-1-i3-a1"
    assert Controller.worker_kind("tmrun-eb-1-stage-a2") == "stage"
    assert Controller.worker_kind("tmrun-eb-1-i3-a1") == "calibrate"


def test_kill_the_controller_during_staging_and_restart_does_not_resubmit(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [{"eb": "slow", "path": str(sdm), "stage_sleep_s": 10, "mode": "ok",
                                    "sleep_s": 1}])
    p = controller(cfg, root, wait=False)
    deadline = time.time() + 30
    while not (root / "tracks" / "slow" / "stage" / "attempt_1.json").exists() and time.time() < deadline:
        time.sleep(0.5)
    time.sleep(2)
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()
    time.sleep(4)                                  # heartbeat goes stale (3 x poll_s)
    controller(cfg, root)
    assert stages(root) == {"slow": "DONE"}
    assert len(stage_submissions(root)) == 1


def test_lost_create_response_for_a_stage_session_is_adopted_by_name(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [{"eb": "lost", "path": str(sdm), "stage_sleep_s": 3, "mode": "ok",
                                    "sleep_s": 1}])
    controller(cfg, root, "--lose-response", "lost")
    assert stages(root) == {"lost": "DONE"}
    assert read_json(root / "tracks" / "lost" / "stage" / "attempt_1.json").get("adopted") is True
    assert len(stage_submissions(root)) == 1


def test_expired_stage_record_is_verified_from_the_manifest(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [{"eb": "gone", "path": str(sdm), "stage_sleep_s": 2, "mode": "ok",
                                    "sleep_s": 1}], poll_s=5)
    controller(cfg, root, env={"TM_FAKE_RETENTION_S": "1"})
    assert stages(root) == {"gone": "DONE"}
    assert len(stage_submissions(root)) == 1


def cfg_project(tmp_path, tracks):
    cfg_dir = FIXTURES / "pipeline_cfg"
    return make_project(tmp_path, tracks, pipeline={
        "image": "local", "resources": {"default": {"cores": None, "ram": None}},
        "worker": {"cmd": sys.executable, "args": "trackmanager/worker.py"},
        "stage_worker": {"cmd": sys.executable},
        "repo": str(PIPELINE_REPO), "source_configs": str(cfg_dir)})


@needs_pipeline
def test_intents_that_agree_with_the_cfgs_pass(tmp_path):
    sdm = sdm_in(tmp_path, scans=[(1, "3C48", ["CALIBRATE_FLUX"]), (2, "TESTA_1", ["OBSERVE_TARGET"])])
    cfg, root = cfg_project(tmp_path, [{"eb": "ok", "path": str(sdm), "mode": "ok", "sleep_s": 1}])
    controller(cfg, root)
    assert stages(root) == {"ok": "DONE"}


@needs_pipeline
@pytest.mark.parametrize("scans, text", [
    ([(1, "3C48", ["CALIBRATE_FLUX"]), (2, "TESTA_1", ["CALIBRATE_PHASE"])], "no OBSERVE_TARGET scan"),
    ([(1, "3C48", ["OBSERVE_TARGET"]), (2, "TESTA_1", ["OBSERVE_TARGET"])], "3C48 was observed as OBSERVE_TARGET"),
])
def test_intents_that_disagree_with_the_cfgs_stop_before_calibration(tmp_path, scans, text):
    sdm = sdm_in(tmp_path, scans=scans)
    cfg, root = cfg_project(tmp_path, [{"eb": "x", "path": str(sdm), "mode": "ok", "sleep_s": 1}])
    controller(cfg, root)
    st = state(root, "x")
    assert (st["stage"], st["failed_from"]) == ("AWAITING_DECISION", "STAGING")
    assert text in st["detail"]
    assert not [r for r in submissions(root) if r.get("kind") == "calibrate"]


def test_request_with_a_path_starts_at_staging(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [])
    subprocess.run([sys.executable, "-m", "trackmanager.request", "--root", str(root), "--eb", "req",
                    "--path", str(sdm), "--mode", "ok", "--sleep-s", "1"], cwd=REPO, check=True,
                   capture_output=True)
    controller(cfg, root, "--max-cycles", "1")
    assert (state(root, "req")["stage"], state(root, "req")["activity"]) == ("STAGING", "running")
    controller(cfg, root)
    assert stages(root) == {"req": "DONE"}


def test_archive_track_is_staged_from_arc_dir_by_its_archive_name(tmp_path):
    from helpers import make_archive_project
    from trackmanager import sdm as sdmlib
    name = "99A-001.sb1001.eb2001.60000.10000000000"
    arc = tmp_path / "arc"
    arc.mkdir()
    sdm = make_fake_sdm(arc, name=name, bdf_bytes=10_000_000,
                        scans=[(1, "TESTA_1", ["OBSERVE_TARGET"]), (2, "3C48", ["CALIBRATE_FLUX"]),
                               (3, "J0000+0000", ["CALIBRATE_PHASE"])])
    cal = sdmlib.byte_size(sdm) / 40e9           # make TAP's 40 GB estimate equal this SDM's size
    gone = "99A-001.sb1006.eb2006.60005.10000000000"      # has no SDM in arc_dir
    cfg, root = make_archive_project(
        tmp_path, "tap_synthetic.csv", ["99A-001"],
        archive={"size_calibration": cal, "include_ebs": [name, gone]},    # tiny SDMs: override skip_candidate
        
        inputs={"source": "arc_path", "arc_dir": str(arc)}, max_workers=0,
        pipeline={"repo": str(PIPELINE_REPO), "source_configs": str(FIXTURES / "pipeline_cfg")})
    controller(cfg, root, "--max-cycles", "12")
    st = state(root, name)
    assert (st["stage"], st["activity"]) == ("CALIBRATING", "queued")        # held by max_workers.calibrate = 0
    assert st["sdm"] == name and 0.99 < st["size_ratio"] < 1.01
    assert read_json(root / "tracks" / name / "stage" / "attempt_1.json")["request"]["env"]["EXPECT_SDM"] == name
    other = state(root, gone)
    assert other["stage"] == "AWAITING_DECISION" and "does not exist" in other["detail"]


def test_disk_gate_runs_tracks_one_at_a_time_when_only_one_fits(tmp_path):
    sdm = sdm_in(tmp_path, bdf_bytes=33_333_333_333)        # three sparse BDFs: a 100 GB SDM
    # 100 GB SDMs, 1.05 TB quota: a calibration grows a track from 100 to 800 GB, so two never fit at once
    cfg, root = project(tmp_path, [{"eb": f"d{i}", "path": str(sdm), "size_gb": 100, "mode": "ok", "sleep_s": 6}
                                   for i in range(3)], max_workers=3, quota_tb=1.05, min_free_tb=0)
    p = controller(cfg, root, wait=False)
    blocked, deadline = {}, time.time() + 90
    while time.time() < deadline and p.poll() is None and not blocked:
        for f in (root / "state").glob("d*.json"):
            s = read_json(f) or {}
            if str(s.get("blocked_by", "")).startswith("disk:"):
                blocked[s["eb"]] = s["blocked_by"]
        time.sleep(0.3)
    assert blocked, "no queued track was ever held by the disk gate"
    assert "0.70 TB" in next(iter(blocked.values()))
    summ = read_json(root / "state" / "summary.json")
    assert summ["disk"]["quota_tb"] == 1.05 and summ["disk"]["modelled_used_tb"] > 0
    out = subprocess.run([sys.executable, "-m", "trackmanager.status", "--root", str(root)], cwd=REPO,
                         capture_output=True, text=True).stdout
    assert "disk       " in out and "of 1.05 TB reserved" in out
    p.communicate(timeout=180)
    assert set(stages(root).values()) == {"DONE"}
    live = peak = 0
    for e in events(root):
        if e.get("activity") == "running" and e.get("from_activity") == "queued" and e["to"] == "CALIBRATING":
            live += 1
        elif e["from"] == "CALIBRATING" and e["to"] != "CALIBRATING":
            live -= 1
        peak = max(peak, live)
    assert peak == 1


def test_without_a_quota_nothing_is_held_back(tmp_path):
    sdm = sdm_in(tmp_path)
    cfg, root = project(tmp_path, [{"eb": f"n{i}", "path": str(sdm), "size_gb": 1e6, "mode": "ok", "sleep_s": 2}
                                   for i in range(2)], max_workers=2)
    controller(cfg, root)
    assert set(stages(root).values()) == {"DONE"}
    assert read_json(root / "state" / "summary.json")["disk"]["quota_tb"] is None
