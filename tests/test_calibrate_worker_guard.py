"""calibrate_worker.sh no longer stages: it needs a verified stage manifest and the SDM it names."""
import json
import os
import signal
import subprocess
import time

from helpers import REPO, make_fake_sdm

SCRIPT = REPO / "workers" / "calibrate_worker.sh"


def run(tmp_path):
    casa = tmp_path / "casa_home" / "bin" / "casa"
    casa.parent.mkdir(parents=True, exist_ok=True)
    casa.write_text("#!/bin/sh\nexit 0\n")
    casa.chmod(0o755)
    env = {**os.environ, "TM_ROOT": str(tmp_path / "run"), "EB": "eb1", "ATTEMPT": "1", "ITERATION": "1",
           "CASA_HOME": str(tmp_path / "casa_home"), "PIPELINE": str(tmp_path / "pipe"),
           "skaha_sessionid": "s1"}
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def manifest(tmp_path, **kw):
    p = tmp_path / "run" / "tracks" / "eb1" / "stage" / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ok": True, "sdm": "x.sb1.eb1.1.2", "bytes": 5, **kw}, indent=1, sort_keys=True))


def test_no_manifest_means_stage_local_has_not_finished(tmp_path):
    code, out = run(tmp_path)
    assert code == 11 and "stage_local has not finished" in out
    assert not (tmp_path / "run/tracks/eb1/worker.lock").exists()          # the lock is released


def test_a_manifest_that_is_not_ok_is_refused(tmp_path):
    manifest(tmp_path, ok=False)
    assert run(tmp_path)[0] == 11


def test_manifest_naming_an_sdm_that_is_not_in_raw_is_refused(tmp_path):
    manifest(tmp_path)
    code, out = run(tmp_path)
    assert code == 11 and "is missing or lacks ASDM.xml" in out


def test_a_verified_sdm_gets_past_the_guard(tmp_path):
    raw = tmp_path / "run" / "tracks" / "eb1" / "raw"
    raw.mkdir(parents=True)
    make_fake_sdm(raw, name="x.sb1.eb1.1.2")
    manifest(tmp_path)
    casa = tmp_path / "casa_home" / "bin" / "casa"
    casa.parent.mkdir(parents=True)
    casa.write_text("#!/bin/sh\nexit 0\n")
    casa.chmod(0o755)
    env = {**os.environ, "TM_ROOT": str(tmp_path / "run"), "EB": "eb1", "ATTEMPT": "1", "ITERATION": "1",
           "CASA_HOME": str(tmp_path / "casa_home"), "PIPELINE": str(tmp_path / "pipe"), "skaha_sessionid": "s1"}
    p = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    log = tmp_path / "run/tracks/eb1/iter_1/logs/attempt_1/stdout.log"
    try:      # the run proceeds into the real work (a fake CASA), so read its log instead of waiting
        deadline = time.time() + 20
        while time.time() < deadline and "workdir" not in (log.read_text() if log.exists() else ""):
            time.sleep(0.2)
        text = log.read_text()
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
    assert "SDM x.sb1.eb1.1.2: 5 bytes (verified by stage_local)" in text and "workdir" in text
