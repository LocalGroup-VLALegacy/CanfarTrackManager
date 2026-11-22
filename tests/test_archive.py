"""Archive sync (plan §3.7): the TAP filter, aggregation, selection, change tracking, downtime."""
import csv
import gzip
import json
import shutil

import pytest

from helpers import FIXTURES, make_archive_project
from trackmanager import archive, config
from trackmanager.store import Root, read_json

REAL = "tap_24B-224.csv.gz"
SYN = "tap_synthetic.csv"


def load(tmp_path, fixture, codes, **kw):
    path, run = make_archive_project(tmp_path, fixture, codes, **kw)
    return config.load(path, check_paths=False), Root(run)


def sheet_rows():
    with open(FIXTURES / "sheet_24B-224.csv") as f:
        return list(csv.DictReader(f))


# ---- the real 24B-224 result ------------------------------------------------------------------
def test_24b224_gives_47_ebs_and_no_image_rows(tmp_path):
    cfg, root = load(tmp_path, REAL, ["24B-224"])
    with gzip.open(FIXTURES / REAL, "rt") as f:
        raw = list(csv.DictReader(f))
    assert any(r["access_format"] == "Image" for r in raw), "fixture must contain image rows"
    rows = archive.client(cfg).query("24B-224")
    assert rows and all(r["access_format"] == "Execution Block" and r["dataproduct_type"] == "visibility" for r in rows)
    agg = archive.aggregate(rows)
    assert len(agg) == 47
    res = archive.sync(root, cfg)
    assert res.ok and len(res.new) == 47 and len(res.kept) == 47 and not res.excluded


def test_24b224_matches_the_old_sheet(tmp_path):
    """The 44 EBs in the old tracker agree with TAP on SDM name and configuration; the other 3
    are reported, like the 14 20A-346 EBs the sheet never listed."""
    cfg, root = load(tmp_path, REAL, ["24B-224"])
    res = archive.sync(root, cfg)
    sheet = {r["Trackname"]: r for r in sheet_rows()}
    assert len(sheet) == 44
    assert set(sheet) <= set(res.kept)
    for name, row in sheet.items():
        rec = res.kept[name]
        assert rec["config"] == row["Configuration"], name
        assert str(rec["ebid"]) == row["EBID"] and str(rec["sbid"]) == row["SBID"], name
        assert row["Target"] in " ".join(rec["fields"]), name
    assert len(set(res.kept) - set(sheet)) == 3


def test_record_fields(tmp_path):
    cfg, root = load(tmp_path, REAL, ["24B-224"])
    archive.sync(root, cfg)
    eb = "24B-224.sb48330789.eb48982136.60857.47877756944"
    rec = read_json(root.archive_json(eb))
    assert rec["sdm"] == eb and rec["ebid"] == 48982136 and rec["sbid"] == 48330789
    assert rec["mjd"] == pytest.approx(60857.47877756944)
    assert rec["config"] == "C" and rec["n_ant"] == 26
    assert rec["est_size_gb"] == pytest.approx(127.397577, rel=1e-6)                # kB / 1e6, once
    assert 0.98 < rec["freq_min_ghz"] < 1.0 and rec["freq_max_ghz"] > 1.8     # Hz in TAP, GHz here
    assert rec["archive_status"] == "LOCKED" and rec["archive_url"].startswith("https://data.nrao.edu/")
    assert rec["duration_min"] > 0 and rec["on_source_s"] > 0 and "3C48" in rec["fields"]
    assert abs(sum(rec["on_source_by_field"].values()) - rec["on_source_s"]) < 0.1
    scans = list(csv.DictReader(open(root.archive_scans(eb))))
    assert len(scans) == rec["n_scans"]


def test_size_is_taken_once_not_summed_over_scans(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"])
    res = archive.sync(root, cfg)
    ok = res.kept["99A-001.sb1001.eb2001.60000.10000000000"]
    assert ok["n_scans"] == 4 and ok["est_size_gb"] == pytest.approx(40.0)      # 40,000,000 kB, once


def test_size_calibration_scales_the_estimate(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"], archive={"size_calibration": 1.5})
    assert archive.sync(root, cfg).kept["99A-001.sb1001.eb2001.60000.10000000000"]["est_size_gb"] == pytest.approx(60.0)


# ---- selection --------------------------------------------------------------------------------
def test_synthetic_selection_and_other_project_ignored(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"])
    res = archive.sync(root, cfg)
    assert sorted(e.split(".")[2] for e in res.kept) == ["eb2001", "eb2002", "eb2003", "eb2005", "eb2006"]
    assert [e.split(".")[2] for e in res.excluded] == ["eb2004"]
    assert "bands_ghz" in next(iter(res.excluded.values()))
    assert not root.archive_json("99A-001.sb1004.eb2004.60003.10000000000").exists()   # out of band: no track
    assert not any("99B" in e for e in res.kept)


def test_include_ebs_beats_band_and_exclude_ebs_wins_over_default(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"],
                     archive={"include_ebs": [2004], "exclude_ebs": ["99A-001.sb1002.eb2002.60001.10000000000"]})
    res = archive.sync(root, cfg)
    assert any("eb2004" in e for e in res.kept)                       # by EB id, past the band filter
    assert [e.split(".")[2] for e in res.excluded] == ["eb2002"]


def test_eb_list_file_is_an_allow_list(tmp_path):
    (tmp_path / "ebs.csv").write_text("eb\n99A-001.sb1001.eb2001.60000.10000000000\n2003\n")
    cfg, root = load(tmp_path, SYN, ["99A-001"], inputs={"eb_list": "file:ebs.csv"})
    cfg.root = tmp_path
    res = archive.sync(root, cfg)
    assert sorted(e.split(".")[2] for e in res.kept) == ["eb2001", "eb2003"]
    assert all(r == "not in inputs.eb_list" or "bands" in r for r in res.excluded.values())


# ---- change tracking --------------------------------------------------------------------------
def retarget(tmp_path, cfg, name):
    cfg.archive.tap_url = f"file://{tmp_path / name}"


def test_resync_is_idempotent_and_changes_are_reported(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"])
    first = archive.sync(root, cfg)
    assert first.new and not first.changed
    snaps = list((root.archive_dir / "snapshots").glob("*.csv.gz"))
    mtime = root.archive_json(first.new[0]).stat().st_mtime_ns
    again = archive.sync(root, cfg)
    assert not again.new and not again.changed and not again.missing
    assert root.archive_json(first.new[0]).stat().st_mtime_ns == mtime           # nothing rewritten
    assert list((root.archive_dir / "snapshots").glob("*.csv.gz")) == snaps       # no new snapshot

    rows = list(csv.DictReader(open(FIXTURES / SYN)))
    for r in rows:
        if "eb2006" in r["obs_publisher_did"]:
            r["proprietary_status"] = "PUBLIC"
    with open(tmp_path / "changed.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    retarget(tmp_path, cfg, "changed.csv")
    third = archive.sync(root, cfg)
    (eb, diff), = third.changed.items()
    assert "eb2006" in eb and diff["archive_status"] == ["LOCKED", "PUBLIC"]
    assert read_json(root.archive_json(eb))["archive_status"] == "PUBLIC"
    assert len(list((root.archive_dir / "snapshots").glob("*.csv.gz"))) == len(snaps) + 1


def test_missing_ebs_are_warned_once_and_never_removed(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"])
    archive.sync(root, cfg)
    rows = [r for r in csv.DictReader(open(FIXTURES / SYN)) if "eb2003" not in r["obs_publisher_did"]]
    with open(tmp_path / "less.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    retarget(tmp_path, cfg, "less.csv")
    res = archive.sync(root, cfg)
    assert [e.split(".")[2] for e in res.missing] == ["eb2003"]
    assert root.archive_json(res.missing[0]).exists()                              # still there
    assert archive.sync(root, cfg).missing == []                                   # warned once
    retarget(tmp_path, cfg, str(FIXTURES / SYN))
    assert archive.sync(root, cfg).ok                                              # it can come back
    assert read_json(root.archive_dir / "index.json")["missing"] == []


# ---- failures ---------------------------------------------------------------------------------
def test_downtime_leaves_everything_and_alarms_after_three(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"])
    archive.sync(root, cfg)
    before = sorted(p.name for p in (root.path / "tracks").iterdir())
    retarget(tmp_path, cfg, "nothing-here.csv")
    for n in (1, 2, 3):
        res = archive.sync(root, cfg)
        assert not res.ok and "cannot read" in res.error
        assert read_json(root.archive_dir / "sync.json")["consecutive_failures"] == n
        assert (archive.alarm(root) is not None) == (n >= 3)
    assert sorted(p.name for p in (root.path / "tracks").iterdir()) == before
    retarget(tmp_path, cfg, str(FIXTURES / SYN))
    assert archive.sync(root, cfg).ok and archive.alarm(root) is None


def test_due_first_cycle_then_after_poll_h_and_sooner_after_a_failure(tmp_path):
    cfg, root = load(tmp_path, SYN, ["99A-001"], archive={"poll_h": 24})
    assert archive.due(root, cfg)
    archive.sync(root, cfg)
    assert not archive.due(root, cfg)
    retarget(tmp_path, cfg, "nothing-here.csv")
    archive.sync(root, cfg)
    assert not archive.due(root, cfg)          # a retry waits at least RETRY_AFTER_S
    sync = read_json(root.archive_dir / "sync.json")
    sync["last_attempt"] = "2000-01-01T00:00:00Z"
    (root.archive_dir / "sync.json").write_text(json.dumps(sync))
    assert archive.due(root, cfg)


VOTABLE = b"""<?xml version="1.0"?><VOTABLE xmlns="http://www.ivoa.net/xml/VOTable/v1.3"><RESOURCE type="results">
<INFO name="QUERY_STATUS" value="%s">%s</INFO><TABLE><FIELD name="obs_publisher_did"/><FIELD name="project_code"/>
<DATA><TABLEDATA><TR><TD>a</TD><TD>X</TD></TR></TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>"""


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_overflow_is_an_error_not_a_short_list(monkeypatch):
    monkeypatch.setattr(archive.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(VOTABLE % (b"OVERFLOW", b"result truncated at MAXREC")))
    with pytest.raises(archive.TapError, match="OVERFLOW"):
        archive.TapClient("https://example.invalid/tap").query("X")


def test_maxrec_sized_answer_is_an_error_even_without_a_status(monkeypatch):
    monkeypatch.setattr(archive.urllib.request, "urlopen", lambda *a, **k: _Resp(VOTABLE % (b"OK", b"")))
    with pytest.raises(archive.TapError, match="MAXREC"):
        archive.TapClient("https://example.invalid/tap", maxrec=1).query("X")
    assert archive.TapClient("https://example.invalid/tap", maxrec=5).query("X") == [{"obs_publisher_did": "a", "project_code": "X"}]


def test_network_error_and_garbage_become_tap_errors(monkeypatch):
    def boom(*a, **k):
        raise archive.urllib.error.URLError("no route")
    monkeypatch.setattr(archive.urllib.request, "urlopen", boom)
    with pytest.raises(archive.TapError, match="request failed"):
        archive.TapClient("https://example.invalid/tap").query("X")
    monkeypatch.setattr(archive.urllib.request, "urlopen", lambda *a, **k: _Resp(b"<html>maintenance"))
    with pytest.raises(archive.TapError, match="not a VOTable"):
        archive.TapClient("https://example.invalid/tap").query("X")


def test_adql_carries_the_required_filter():
    q = archive.adql("24B-224")
    assert "project_code = '24B-224'" in q and "access_format = 'Execution Block'" in q
    assert "dataproduct_type = 'visibility'" in q and "FROM tap_schema.obscore" in q


def test_did_parsing_including_the_older_underscore_names():
    new = archive.parse_did("20A-346.sb38098105.eb38158028.58985.68987263889")
    assert (new["sbid"], new["ebid"], new["mjd"]) == (38098105, 38158028, 58985.68987263889)
    old = archive.parse_did("20A-346_sb42966433_2_1.59886.97117081018")
    assert old["sdm"].startswith("20A-346_sb") and old["sbid"] == 42966433 and old["ebid"] is None
    assert archive.parse_did("weird") == {"sdm": "weird", "ebid": None, "sbid": None, "mjd": None}


def test_spw_signature_ignores_doppler_jitter_in_the_center_frequencies():
    row = {"spw_names": "A,B", "bandwidths": "2e6,2e6", "nums_channels": "128,128",
           "spectral_resolutions": "15625.0,15625.0", "center_frequencies": "1374535140.07,1420326206.13"}
    moved = dict(row, center_frequencies="1374600000.00,1420400000.00")
    assert archive._spw_signature(row) == archive._spw_signature(moved)
    assert archive._spw_signature(row) != archive._spw_signature(dict(row, nums_channels="128,256"))


def test_24b224_has_a_few_setups_not_one_per_eb(tmp_path):
    cfg, root = load(tmp_path, REAL, ["24B-224"])
    sigs = {r["spw_signature"] for r in archive.sync(root, cfg).kept.values()}
    assert len(sigs) == 3
