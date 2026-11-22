"""trackmanager/stage_worker.py run directly, the way a session runs it."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import REPO, make_fake_sdm, make_fake_tar
from trackmanager import sdm as sdmlib

WORKER = REPO / "trackmanager" / "stage_worker.py"


def run(tmp_path, input_path, attempt=1, session="s1", **env):
    e = {**os.environ, "TM_ROOT": str(tmp_path / "run"), "EB": "eb1", "ATTEMPT": str(attempt),
         "skaha_sessionid": session, "INPUT_PATH": str(input_path)}
    e.update({k: str(v) for k, v in env.items()})
    return subprocess.run([sys.executable, str(WORKER)], env=e, capture_output=True, text=True)


def manifest(tmp_path):
    return json.loads((tmp_path / "run/tracks/eb1/stage/manifest.json").read_text())


def tree_state(p):
    return sorted((str(f.relative_to(p)), f.stat().st_size, f.stat().st_mtime_ns) for f in Path(p).rglob("*"))


def test_directory_is_symlinked_not_copied_and_the_input_is_untouched(tmp_path):
    (tmp_path / "in").mkdir()
    sdm = make_fake_sdm(tmp_path / "in")
    before = tree_state(sdm)
    r = run(tmp_path, sdm, EXPECT_SDM=sdm.name)
    assert r.returncode == 0, r.stdout + r.stderr
    raw = tmp_path / "run/tracks/eb1/raw" / sdm.name
    assert raw.is_symlink() and os.path.realpath(raw) == os.path.realpath(sdm)
    m = manifest(tmp_path)
    assert m["ok"] and m["mode"] == "symlink" and m["sdm"] == sdm.name
    assert m["main_rows"] == m["bdf_files"] == 3 and m["bytes"] == sdmlib.byte_size(sdm)
    assert m["fields"]["IC10_1_CTR"]["intents"] == ["OBSERVE_TARGET"]
    assert tree_state(sdm) == before
    run_json = json.loads((tmp_path / "run/tracks/eb1/stage/logs/attempt_1/run.json").read_text())
    assert run_json["exit_code"] == 0 and run_json["sdm_bytes"] == m["bytes"]
    assert not (tmp_path / "run/tracks/eb1/worker.lock").exists()


def test_tar_is_unpacked_through_partial_and_leaves_the_tar_alone(tmp_path):
    (tmp_path / "src").mkdir()
    sdm = make_fake_sdm(tmp_path / "src")
    tar = make_fake_tar(tmp_path / "x.tar", sdm)
    before = tar.read_bytes()
    r = run(tmp_path, tar, EXPECT_SDM=sdm.name)
    assert r.returncode == 0, r.stdout + r.stderr
    raw = tmp_path / "run/tracks/eb1/raw"
    assert (raw / sdm.name / "Main.xml").is_file() and not (raw / sdm.name).is_symlink()
    assert not (raw / ".partial").exists()
    assert manifest(tmp_path)["mode"] == "tar" and manifest(tmp_path)["ok"]
    assert tar.read_bytes() == before


def test_a_valid_manifest_means_nothing_to_do_and_a_rerun_after_failure_works(tmp_path):
    (tmp_path / "in").mkdir()
    sdm = make_fake_sdm(tmp_path / "in")
    assert run(tmp_path, sdm).returncode == 0
    first = (tmp_path / "run/tracks/eb1/stage/manifest.json").read_text()
    r = run(tmp_path, sdm, attempt=2, session="s2")
    assert r.returncode == 0 and "nothing to do" in r.stdout
    assert (tmp_path / "run/tracks/eb1/stage/manifest.json").read_text() == first


@pytest.mark.parametrize("what, expect_code, text", [
    ("bdf", 11, "BDF files"),
    ("name", 11, "archive says"),
    ("size", 12, "archive estimate"),
    ("missing", 10, "does not exist"),
    ("xml", 11, "Main.xml is missing"),
])
def test_failed_checks_write_a_not_ok_manifest_with_the_reason(tmp_path, what, expect_code, text):
    (tmp_path / "in").mkdir()
    sdm = make_fake_sdm(tmp_path / "in", bdf_files=2 if what == "bdf" else None)
    env = {"EXPECT_SDM": "other-name" if what == "name" else sdm.name}
    if what == "size":
        env["EST_BYTES"] = sdmlib.byte_size(sdm) * 2
    inp = tmp_path / "nowhere" if what == "missing" else sdm
    if what == "xml":
        (sdm / "Main.xml").unlink()
    r = run(tmp_path, inp, **env)
    assert r.returncode == expect_code, r.stdout + r.stderr
    m = manifest(tmp_path)
    assert m["ok"] is False and any(text in p for p in m["problems"])
    assert not (tmp_path / "run/tracks/eb1/worker.lock").exists()     # lock released on failure


def test_size_within_tolerance_passes_and_records_the_ratio(tmp_path):
    (tmp_path / "in").mkdir()
    sdm = make_fake_sdm(tmp_path / "in")
    est = sdmlib.byte_size(sdm) * 1.02
    assert run(tmp_path, sdm, EST_BYTES=est, SIZE_TOLERANCE=0.05).returncode == 0
    assert 0.97 < manifest(tmp_path)["ratio"] < 1.0
    assert run(tmp_path, sdm, EST_BYTES=est, SIZE_TOLERANCE=0.05).returncode == 0


def test_second_worker_is_refused_while_the_lock_is_held(tmp_path):
    (tmp_path / "in").mkdir()
    sdm = make_fake_sdm(tmp_path / "in")
    lock = tmp_path / "run/tracks/eb1/worker.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("other-session")
    r = run(tmp_path, sdm)
    assert r.returncode == 3 and "held by other-session" in r.stdout
    assert lock.read_text() == "other-session" and not (tmp_path / "run/tracks/eb1/stage/manifest.json").exists()


def test_tar_with_the_wrong_name_or_two_directories_fails(tmp_path):
    (tmp_path / "src").mkdir()
    sdm = make_fake_sdm(tmp_path / "src")
    tar = make_fake_tar(tmp_path / "x.tar", sdm)
    assert run(tmp_path, tar, EXPECT_SDM="not-this").returncode == 11
    assert not (tmp_path / "run/tracks/eb1/raw" / sdm.name).exists()


def test_stale_partial_from_a_killed_attempt_is_cleared(tmp_path):
    (tmp_path / "src").mkdir()
    sdm = make_fake_sdm(tmp_path / "src")
    tar = make_fake_tar(tmp_path / "x.tar", sdm)
    junk = tmp_path / "run/tracks/eb1/raw/.partial/old"
    junk.mkdir(parents=True)
    assert run(tmp_path, tar).returncode == 0
    assert not junk.exists()


def test_fault_injection_modes(tmp_path):
    (tmp_path / "in").mkdir()
    sdm = make_fake_sdm(tmp_path / "in")
    assert run(tmp_path, sdm, MODE="fail").returncode == 1
    assert run(tmp_path, sdm, MODE="fail_first", attempt=1).returncode == 1
    assert run(tmp_path, sdm, MODE="no_result", attempt=2).returncode == 0
    assert not (tmp_path / "run/tracks/eb1/stage/manifest.json").exists()
    assert run(tmp_path, sdm, MODE="fail_first", attempt=2).returncode == 0
