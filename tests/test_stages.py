"""Stage-machine behaviour added in M1 (driver tests on the fake backend): the publishing
activity, retry back to the stage that failed, NEEDS_MANUAL, and limits that change mid-run."""
import os
import stat
import subprocess
import sys
import time

import yaml

from helpers import make_project
from test_fake_backend import SKEL, controller, read_json, read_jsonl, stages, submissions


def decide(root, eb, action, *extra):
    subprocess.run([sys.executable, "-m", "trackmanager.decide", "--root", str(root), "--eb", eb,
                    "--action", action, *extra], cwd=SKEL, check=True, capture_output=True)


def events(root):
    return read_jsonl(root / "events.jsonl")


def fake_setfacl(tmp_path, fail_calls):
    """A setfacl on PATH that fails its first `fail_calls` calls (-1: always), counting calls."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "setfacl"
    script.write_text(f"""#!/bin/sh
n=$(cat "{tmp_path}/setfacl.count" 2>/dev/null || echo 0)
n=$((n + 1)); echo $n > "{tmp_path}/setfacl.count"
if [ {fail_calls} -lt 0 ] || [ $n -le {fail_calls} ]; then echo "Operation not permitted" >&2; exit 1; fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bindir}:{os.environ['PATH']}"}


def with_qa_products(root, eb):
    qd = root / "products" / eb / "iter_1"
    qd.mkdir(parents=True)
    (qd / "index.html").write_text("<html></html>")


def test_publish_failure_is_retried_next_cycle_and_then_proceeds(tmp_path):
    cfg, root = make_project(tmp_path, [{"eb": "p", "mode": "ok", "sleep_s": 1}], qa={"group": "someone"})
    with_qa_products(root, "p")
    controller(cfg, root, env=fake_setfacl(tmp_path, fail_calls=2))
    assert stages(root) == {"p": "DONE"}
    failed = [e for e in events(root) if "publishing iteration 1 failed" in e["reason"]]
    assert len(failed) == 1                      # the same error twice is one event
    assert failed[0]["activity"] == "publishing"
    assert len(submissions(root)) == 1


def test_publish_that_keeps_failing_asks_the_operator_and_retry_republishes(tmp_path):
    cfg, root = make_project(tmp_path, [{"eb": "p", "mode": "ok", "sleep_s": 1}], qa={"group": "someone"})
    with_qa_products(root, "p")
    controller(cfg, root, env=fake_setfacl(tmp_path, fail_calls=-1))
    st = read_json(root / "state" / "p.json")
    assert st["stage"] == "AWAITING_DECISION" and st["reason"] == "failed"
    assert (st["failed_from"], st["failed_activity"]) == ("CALIBRATING", "publishing")
    assert st["publish_failures"] == 5 and "setfacl" in st["detail"]
    # access fixed: retry goes back to publishing, not to a new calibration
    decide(root, "p", "retry")
    controller(cfg, root, env=fake_setfacl(tmp_path, fail_calls=0))
    st = read_json(root / "state" / "p.json")
    assert st["stage"] == "DONE" and "failed_from" not in st and "publish_error" not in st
    assert len(submissions(root)) == 1
    assert ("AWAITING_DECISION", "CALIBRATING") in {(e["from"], e["to"]) for e in events(root)}


def test_retry_of_a_failed_promotion_retries_promotion_not_calibration(tmp_path):
    cfg, root = make_project(tmp_path, [{"eb": "c", "mode": "ok", "sleep_s": 1}])
    clash = root / "science" / "c" / "c.ms.split"
    clash.mkdir(parents=True)
    controller(cfg, root)
    st = read_json(root / "state" / "c.json")
    assert (st["stage"], st["failed_from"]) == ("AWAITING_DECISION", "PROMOTING")
    clash.rename(tmp_path / "old")
    decide(root, "c", "retry")
    controller(cfg, root)
    assert stages(root) == {"c": "DONE"}
    assert len(submissions(root)) == 1
    assert ("AWAITING_DECISION", "PROMOTING") in {(e["from"], e["to"]) for e in events(root)}


def test_needs_manual_stops_automation_for_that_track(tmp_path):
    cfg, root = make_project(tmp_path, [{"eb": "m", "mode": "fail", "sleep_s": 1},
                                        {"eb": "ok", "mode": "ok", "sleep_s": 1}], auto_retry=0)
    controller(cfg, root)
    assert stages(root) == {"m": "AWAITING_DECISION", "ok": "DONE"}
    decide(root, "m", "needs_manual", "--notes", "re-observe with the VLA")
    controller(cfg, root)
    st = read_json(root / "state" / "m.json")
    assert st["stage"] == "NEEDS_MANUAL" and "re-observe" in st["detail"]
    assert len(submissions(root)) == 2           # nothing was submitted for it again
    decide(root, "m", "retry")                   # ignored: not in AWAITING_DECISION any more
    controller(cfg, root)
    assert read_json(root / "state" / "m.json")["stage"] == "NEEDS_MANUAL"
    assert len(submissions(root)) == 2


def test_retry_after_a_failed_run_queues_calibration_again(tmp_path):
    cfg, root = make_project(tmp_path, [{"eb": "a", "mode": "fail_first", "sleep_s": 1}], auto_retry=0)
    controller(cfg, root)
    st = read_json(root / "state" / "a.json")
    assert (st["failed_from"], st["failed_activity"], st["failure_class"]) == ("CALIBRATING", "running", "failed")
    decide(root, "a", "retry")
    controller(cfg, root)
    assert stages(root) == {"a": "DONE"} and len(submissions(root)) == 2


def test_raising_a_limit_mid_run_releases_the_held_tracks(tmp_path):
    cfg, root = make_project(tmp_path, [{"eb": "a", "mode": "ok", "sleep_s": 1},
                                        {"eb": "b", "mode": "ok", "sleep_s": 1}], max_workers=0)
    p = controller(cfg, root, wait=False)
    deadline = time.time() + 30
    seen = None
    while time.time() < deadline and seen is None:
        s = read_json(root / "state" / "summary.json", {})
        if s and all(st.get("blocked_by") for st in s["ebs"].values()) and len(s["ebs"]) == 2:
            seen = s
        time.sleep(0.3)
    assert seen, "tracks never showed as blocked"
    assert {st["blocked_by"] for st in seen["ebs"].values()} == {"max_workers.calibrate (0/0)"}
    assert seen["workers"]["limit"] == 0 and seen["workers"]["queued"] == 2
    assert not submissions(root)
    data = yaml.safe_load(cfg.read_text())
    data["controller"]["max_workers"] = {"calibrate": 2}
    cfg.write_text(yaml.safe_dump(data))         # the controller re-reads this every cycle
    p.communicate(timeout=60)
    assert stages(root) == {"a": "DONE", "b": "DONE"}
    assert all("blocked_by" not in read_json(root / "state" / f"{e}.json") for e in "ab")
