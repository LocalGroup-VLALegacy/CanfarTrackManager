"""M4: what the Sheets bridge shows for a track, and the metadata flag rows are checked against:
scans (archive_scans.csv), the SPW count (TAP spw_names), antenna names (Antenna.xml via the
stage manifest); plus the silent fill-in of fields added to archive records."""
import pytest

from helpers import FIXTURES, make_archive_project
from trackmanager import archive, config, flagcmds, sdm
from trackmanager.sheet_context import archive_ctx, detail, mjd_to_utc, track_context
from trackmanager.store import Root, read_json, write_json

EB = "24B-224.sb48330789.eb48982136.60857.47877756944"


def synced(tmp_path):
    path, run = make_archive_project(tmp_path, "tap_24B-224.csv.gz", ["24B-224"])
    cfg = config.load(path, check_paths=False)
    root = Root(run)
    archive.sync(root, cfg)
    return cfg, root


def test_ic10_antennas_from_the_real_table():
    names = sdm.antennas(FIXTURES / "sdm_ic10")
    assert len(names) == 27 and names[0] == "ea01" and "ea20" not in names and names[-1] == "ea28"
    assert sdm.antennas(FIXTURES) == []                 # no Antenna.xml: nothing, not an error


def test_archive_record_has_the_spw_count_and_old_records_fill_it_in_silently(tmp_path):
    cfg, root = synced(tmp_path)
    rec = read_json(root.archive_json(EB))
    assert rec["n_spw"] > 10
    old = {k: v for k, v in rec.items() if k != "n_spw"}   # a record written before n_spw existed
    write_json(root.archive_json(EB), old)
    res = archive.sync(root, cfg)
    assert EB not in res.changed                          # not reported as a metadata change ...
    assert read_json(root.archive_json(EB))["n_spw"] == rec["n_spw"]   # ... but filled in


def test_context_from_archive_preflight_manifest_and_jobs(tmp_path):
    cfg, root = synced(tmp_path)
    write_json(root.manifest(EB), {"ok": True, "bytes": 127_400_000_000,
                                   "antennas": [f"ea{i:02d}" for i in range(1, 29) if i != 20]})
    st = {"eb": EB, "stage": "AWAITING_REVIEW", "iteration": 2, "detail": "verified", "hold": False,
          "updated": "2026-11-24T03:01:29Z", "preflight": {"targets": ["IC342"]},
          "qa_url": "https://ws-uv.canfar.net/arc/files/x/iter_2/index.html"}
    (root.inputs(EB, 2)).mkdir(parents=True)
    (root.inputs(EB, 2) / "refant_ignore.txt").write_text("ea26\n")
    write_json(root.review(EB, 1), {"verdict": "restart", "verdict_word": "RESTART"})
    jobs = {"a": {"eb": EB, "kind": "calibrate", "submitted": "2026-11-20T00:00:00Z", "job_id": "old"},
            "b": {"eb": EB, "kind": "calibrate", "submitted": "2026-11-23T00:00:00Z", "job_id": "new",
                  "wall_h": 5.5, "peak_disk_gb": 900.0},
            "c": {"eb": EB, "kind": "stage", "submitted": "2026-11-24T00:00:00Z", "job_id": "stg"}}
    ctx = track_context(root, cfg, st, jobs, {"signup": {"reviewer": "R", "signed_up": "2026-11-23 10:00:00"},
                                              "ack": "ok", "tab_url": "https://docs.google.com/x#gid=5"})
    a = ctx["archive"]
    assert a["project"] == "24B-224" and a["config"] == "C" and a["target"] == "IC342"
    assert a["date"] == "2025-07-01" and a["start"].startswith("2025-07-01 ") and len(a["start"]) == 19
    assert a["scans"][0] == 1 and len(a["scans"]) == len(set(a["scans"])) > 5
    assert a["n_spw"] > 10 and len(a["antennas"]) == 27 and a["on_source_min"] > 0
    assert ctx["state"]["updated"] == "2026-11-24 03:01:29"
    assert ctx["job"]["job_id"] == "new" and ctx["run"]["sdm_gb"] == 127.4
    assert ctx["review"]["verdict"] == "RESTART (iteration 1)" and ctx["review"]["refant_ignore"] == "ea26"
    assert ctx["links"]["flag_tab"].endswith("gid=5") and ctx["links"]["qa_track"] == ""   # no history page yet
    # flag rows are checked against this metadata
    row = {"Apply Row": True, "Issue Description": "x", "Name of QA person": "R"}
    assert flagcmds.check_row({**row, "Scan": "999"}, a)
    assert flagcmds.check_row({**row, "SPW": str(a["n_spw"])}, a)
    assert flagcmds.check_row({**row, "Antenna Name": "ea20"}, a)
    assert not flagcmds.check_row({**row, "Antenna Name": "ea26", "Scan": "1", "SPW": "0"}, a)
    assert flagcmds.check_row({**row, "Start Date/Time": "2025/07/02/12:00:00"}, a)   # outside the track


def test_config_track_without_archive_record(tmp_path):
    cfg, root = synced(tmp_path)
    st = {"eb": "ic10d", "stage": "CALIBRATING", "iteration": 1, "hold": True, "hold_reason": None, "detail": "x"}
    a = archive_ctx(root, st, {"config": "D", "size_gb": 16.7})
    assert (a["sdm"], a["config"], a["est_size_gb"], a["fields"], a["scans"]) == ("ic10d", "D", 16.7, [], [])
    assert detail(st) == "HOLD: operator -- x"
    assert mjd_to_utc(None) is None and mjd_to_utc(51544.5).isoformat() == "2000-01-01T12:00:00"
