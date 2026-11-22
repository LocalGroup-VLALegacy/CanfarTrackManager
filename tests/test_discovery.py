"""Archive tracks through the real controller (fake backend): sync -> DISCOVERED -> preflight ->
AWAITING_DATA / STAGING / AWAITING_DECISION, and the operator's include / recheck."""
import shutil

import yaml

from helpers import FIXTURES, REPO, make_archive_project
from test_fake_backend import SKEL, controller, read_json, read_jsonl, submissions
from test_stages import decide, events

PIPELINE_REPO = REPO.parent / "ReductionPipeline"


def project(tmp_path, source="nrao_link", cfg_dir=None, **kw):
    cfg_dir = cfg_dir or FIXTURES / "pipeline_cfg"
    return make_archive_project(tmp_path, "tap_synthetic.csv", ["99A-001"], inputs={"source": source},
                                pipeline={"repo": str(PIPELINE_REPO), "source_configs": str(cfg_dir)}, **kw)


def st(root, n):
    return read_json(root / "state" / f"99A-001.sb{1000 + n}.eb{2000 + n}.{59999 + n}.10000000000.json")


def run(cfg, root, cycles=2, **kw):
    return controller(cfg, root, "--max-cycles", str(cycles), **kw)


def test_archive_ebs_are_discovered_and_preflighted_to_their_gates(tmp_path):
    cfg, root = project(tmp_path)
    p = run(cfg, root)
    assert p.returncode == 0, p.output
    ok = st(root, 1)
    assert (ok["stage"], ok["request_state"]) == ("AWAITING_DATA", "needs_request")
    assert ok["archive_url"].startswith("https://data.nrao.edu/") and ok["spec"] == {"config": "C", "source": "archive"}
    assert ok["preflight"]["verdict"] == "ok" and ok["est_size_gb"] == 40.0
    short = st(root, 2)
    assert (short["stage"], short["reason"]) == ("AWAITING_DECISION", "skip_candidate")
    assert "min_duration_min" in short["detail"] and "failed_from" not in short
    bad = st(root, 3)
    assert (bad["stage"], bad["reason"], bad["failed_from"]) == ("AWAITING_DECISION", "preflight_failed", "DISCOVERED")
    assert "MYSTERY_1" in bad["detail"]
    assert st(root, 5)["reason"] == "skip_candidate"                     # estimated size 0
    assert st(root, 6)["stage"] == "AWAITING_DATA"
    assert st(root, 4) is None                                           # out of band: never a track
    assert (root / "tracks" / "99A-001.sb1001.eb2001.60000.10000000000" / "archive.json").exists()
    assert not submissions(root)                                         # nothing is staged or calibrated yet
    evs = events(root)
    assert any(e["reason"] == "found by archive sync" and e["to"] == "DISCOVERED" for e in evs)
    summ = read_json(root / "state" / "summary.json")
    assert summ["archive"]["ebs"] == 5 and summ["archive"]["alarm"] is None
    assert summ["ebs"][ok["eb"]]["est_size_gb"] == 40.0


def test_arc_path_tracks_go_to_staging_and_wait_for_a_stage_worker(tmp_path):
    cfg, root = project(tmp_path, source="arc_path")
    run(cfg, root)
    s = st(root, 1)
    assert (s["stage"], s["activity"]) == ("STAGING", "queued")
    assert s["blocked_by"] and s["queue_position"] >= 1                           # waits in the database
    assert not submissions(root)


def test_include_lets_a_skip_candidate_through_and_retry_does_not_apply(tmp_path):
    cfg, root = project(tmp_path)
    run(cfg, root)
    eb = st(root, 2)["eb"]
    decide(root, eb, "retry")                       # a skip candidate has nothing to retry
    run(cfg, root, cycles=1)
    assert st(root, 2)["stage"] == "AWAITING_DECISION"
    assert any(f.read_text().count("ignored: retry does not apply") for f in (root / "inbox" / "archive").glob("*.json"))
    decide(root, eb, "include")
    run(cfg, root, cycles=2)
    s = st(root, 2)
    assert s["stage"] == "AWAITING_DATA" and s["included"] is True and "reason" not in s
    assert "included by the operator" in s["detail"]
    assert ("AWAITING_DECISION", "DISCOVERED") in {(e["from"], e["to"]) for e in events(root)}


def test_include_does_not_apply_to_a_failed_preflight(tmp_path):
    cfg, root = project(tmp_path)
    run(cfg, root)
    decide(root, st(root, 3)["eb"], "include")
    run(cfg, root, cycles=1)
    assert st(root, 3)["reason"] == "preflight_failed"


def test_recheck_after_a_cfg_fix_moves_a_failed_preflight_on(tmp_path):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(FIXTURES / "pipeline_cfg", cfg_dir)
    cfg, root = project(tmp_path, cfg_dir=cfg_dir)
    run(cfg, root)
    eb = st(root, 3)["eb"]
    assert st(root, 3)["reason"] == "preflight_failed"
    (cfg_dir / "test_targets_vsys.cfg").write_text("[target_vsys_kms]\nTESTA = -100\nMYSTERY = 5\n")
    decide(root, eb, "recheck")
    run(cfg, root, cycles=2)
    s = st(root, 3)
    assert s["stage"] == "AWAITING_DATA" and "reason" not in s and "failed_from" not in s
    assert any("operator recheck" in e["reason"] for e in events(root) if e["eb"] == eb)


def test_unreadable_cfgs_stop_at_a_decision_not_a_crash(tmp_path):
    cfg, root = project(tmp_path, cfg_dir=tmp_path / "no-such-dir")
    assert run(cfg, root).returncode == 0
    s = st(root, 1)
    assert (s["stage"], s["reason"]) == ("AWAITING_DECISION", "preflight_failed")
    assert "cannot read the cfgs" in s["detail"]


def test_tap_downtime_makes_no_tracks_and_raises_the_alarm_after_repeats(tmp_path):
    cfg, root = project(tmp_path)
    data = yaml.safe_load(cfg.read_text())
    data["archive"]["tap_url"] = f"file://{tmp_path}/down.csv"
    cfg.write_text(yaml.safe_dump(data))
    (root / "archive").mkdir(parents=True)
    (root / "archive" / "sync.json").write_text(
        '{"consecutive_failures": 2, "last_attempt": "2000-01-01T00:00:00Z", "last_ok": "1999-12-31T00:00:00Z"}')
    assert run(cfg, root, cycles=2).returncode == 0     # the heartbeat of cycle 2 reports cycle 1's sync
    assert not list((root / "state").glob("99A*.json"))
    hb = read_json(root / "controller" / "heartbeat.json")
    assert hb["archive_alarm"]["consecutive_failures"] == 3 and "cannot read" in hb["archive_alarm"]["error"]
    assert any(e["to"] == "archive" and "sync failed (3 in a row)" in e["reason"] for e in events(root))
    assert read_json(root / "state" / "summary.json")["archive"]["alarm"]["consecutive_failures"] == 3


def test_second_sync_is_not_due_within_poll_h_and_config_tracks_still_work(tmp_path):
    cfg, root = project(tmp_path, tracks=[{"eb": "cfgtrack", "mode": "ok", "sleep_s": 1}], review=False)
    run(cfg, root, cycles=4)
    assert read_json(root / "state" / "cfgtrack.json")["stage"] in ("CALIBRATING", "PROMOTING", "DONE")
    n_found = sum(e["reason"] == "found by archive sync" for e in events(root))
    assert n_found == 5                                   # synced once, not every cycle
