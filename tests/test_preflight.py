"""Preflight (plan §3.7): decided from TAP metadata and the project cfgs, before any download."""
import shutil

import pytest

from helpers import FIXTURES, REPO, make_archive_project
from trackmanager import archive, cfgs, config, preflight
from trackmanager.store import Root

PIPELINE_REPO = REPO.parent / "ReductionPipeline"
pytestmark = pytest.mark.skipif(not (PIPELINE_REPO / "lband_pipeline" / "read_config_files.py").exists(),
                                reason="needs the ReductionPipeline checkout next to this repo")


def setup(tmp_path, fixture="tap_synthetic.csv", codes=("99A-001",), cfg_dir=None, **kw):
    path, run = make_archive_project(
        tmp_path, fixture, list(codes), pipeline={"repo": str(PIPELINE_REPO),
                                                  "source_configs": str(cfg_dir or FIXTURES / "pipeline_cfg")}, **kw)
    cfg = config.load(path, check_paths=False)
    res = archive.sync(Root(run), cfg)
    return cfg, res.kept, cfgs.load(cfg)


def by_eb(cfg, kept, tables, **kw):
    sig = preflight.main_signature(kept.values())
    return {k.split(".")[2]: preflight.run(v, cfg, tables, main_sig=sig, **kw) for k, v in kept.items()}


def levels(result):
    return {c.check: c.level for c in result.checks}


def test_each_synthetic_eb_gets_its_verdict(tmp_path):
    cfg, kept, tables = setup(tmp_path)
    r = by_eb(cfg, kept, tables)
    assert r["eb2001"].verdict == "ok" and r["eb2001"].targets == ["TESTA"]
    assert r["eb2001"].fields == {"TESTA_1": "target", "3C48": "calibrator", "J0000+0000": "calibrator"}
    assert r["eb2002"].verdict == "skip_candidate" and levels(r["eb2002"])["duration"] == "skip_candidate"
    assert r["eb2003"].verdict == "preflight_failed" and levels(r["eb2003"])["fields"] == "fail"
    assert "MYSTERY_1" in r["eb2003"].checks[0].detail
    assert r["eb2005"].verdict == "skip_candidate" and levels(r["eb2005"])["size"] == "skip_candidate"
    assert r["eb2006"].verdict == "ok"
    assert levels(r["eb2006"])["access"] == "info" and levels(r["eb2006"])["spw_setup"] == "warn"


def test_target_removed_from_the_cfg_fails_before_any_download(tmp_path):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(FIXTURES / "pipeline_cfg", cfg_dir)
    (cfg_dir / "test_targets_vsys.cfg").write_text("[target_vsys_kms]\nOTHER = 1\n")
    cfg, kept, tables = setup(tmp_path, cfg_dir=cfg_dir)
    r = by_eb(cfg, kept, tables)
    assert r["eb2001"].verdict == "preflight_failed"
    assert "TESTA_1" in r["eb2001"].summary()


def test_included_bypasses_skip_candidates_but_not_failures(tmp_path):
    cfg, kept, tables = setup(tmp_path)
    r = by_eb(cfg, kept, tables, included=True)
    assert r["eb2002"].verdict == "ok" and r["eb2005"].verdict == "ok"
    assert r["eb2003"].verdict == "preflight_failed"


def test_min_duration_comes_from_the_config(tmp_path):
    cfg, kept, tables = setup(tmp_path, archive={"min_duration_min": 5})
    assert by_eb(cfg, kept, tables)["eb2002"].verdict == "ok"       # 10 min >= 5


def test_vrange_and_calibrator_hi_warnings(tmp_path):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(FIXTURES / "pipeline_cfg", cfg_dir)
    (cfg_dir / "test_targets_vrange.cfg").write_text("[OTHER]\nHI = 1,-1\n")
    cfg, kept, tables = setup(tmp_path, cfg_dir=cfg_dir)
    ok = by_eb(cfg, kept, tables)["eb2001"]
    lv = levels(ok)
    assert lv["vrange"] == "warn" and lv["calibrator_hi"] == "warn"        # J0000+0000 has no HI range
    assert "J0000+0000" in next(c for c in ok.checks if c.check == "calibrator_hi").detail
    assert ok.verdict == "ok"                                               # warnings never block


def test_calibrator_only_eb_is_a_skip_candidate_not_a_cfg_failure(tmp_path):
    cfg, kept, tables = setup(tmp_path)
    rec = dict(next(iter(kept.values())), fields=["3C48", "J0000+0000"])
    assert preflight.run(rec, cfg, tables).verdict == "skip_candidate"
    assert preflight.run(rec, cfg, tables, included=True).verdict == "ok"


def test_summary_text(tmp_path):
    cfg, kept, tables = setup(tmp_path)
    r = by_eb(cfg, kept, tables)
    assert r["eb2001"].summary() == "ok (1 warning)"        # J0000+0000 has no HI range in the cfg
    assert r["eb2002"].summary().startswith("duration: 10 min < min_duration_min (30)")
    assert r["eb2006"].summary() == "ok (1 warning)"


def test_cfgs_read_errors_are_reported_not_raised_raw(tmp_path):
    path, run = make_archive_project(tmp_path, "tap_synthetic.csv", ["99A-001"],
                                     pipeline={"repo": str(PIPELINE_REPO), "source_configs": str(tmp_path / "nope")})
    with pytest.raises(cfgs.CfgError):
        cfgs.load(config.load(path, check_paths=False))
    path, _ = make_archive_project(tmp_path, "tap_synthetic.csv", ["99A-001"],
                                   pipeline={"repo": str(tmp_path / "no-repo")})
    with pytest.raises(cfgs.CfgError, match="not found"):
        cfgs.load(config.load(path, check_paths=False))


def test_cfgs_restore_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(cfgs.ENV, "/somewhere/else")
    setup(tmp_path)
    assert __import__("os").environ[cfgs.ENV] == "/somewhere/else"
    monkeypatch.delenv(cfgs.ENV)
    setup(tmp_path / "again" if (tmp_path / "again").mkdir() is None else tmp_path)
    assert cfgs.ENV not in __import__("os").environ


def test_the_matching_rule_is_the_pipelines():
    """Preflight copies target_setup.identify_targets' substring rule (it needs casatools, so
    it cannot be imported). Fail here if the pipeline's rule moves."""
    src = (PIPELINE_REPO / "lband_pipeline" / "target_setup.py").read_text()
    body = src.split("def identify_targets", 1)[1].split("\ndef ", 1)[0]
    assert "for gal in target_vsys_kms:" in body and "if gal in field:" in body
    assert preflight.match_target("IC10_1(CTR)", {"M31": 1, "IC10": 2}) == "IC10"
    assert preflight.match_target("M31LARGE_5", {"M31": 1, "IC10": 2}) == "M31"
    assert preflight.match_target("NGC253", {"M31": 1}) is None


def test_24b224_against_the_real_lglbs_cfgs(tmp_path):
    """The real project: IC342 is a target, 3C48/3C138 are calibrators in the cfg, the J-names are
    hinted calibrators; nothing fails, and the two near-empty EBs are the skip candidates."""
    cfg, kept, tables = setup(tmp_path, fixture="tap_24B-224.csv.gz", codes=("24B-224",),
                              cfg_dir=PIPELINE_REPO / "config_files")
    res = {k: preflight.run(v, cfg, tables, main_sig=preflight.main_signature(kept.values())) for k, v in kept.items()}
    assert len(res) == 47
    assert not [k for k, r in res.items() if r.verdict == "preflight_failed"], \
        {k: r.summary() for k, r in res.items() if r.verdict == "preflight_failed"}
    skips = {k: kept[k]["est_size_gb"] for k, r in res.items() if r.verdict == "skip_candidate"}
    assert len(skips) == 3 and all(v < 1.0 or kept[k]["duration_min"] < 30 for k, v in skips.items())
    assert any("calibrators only" in res[k].summary() for k in skips)
    assert all(r.targets == ["IC342"] for r in res.values() if r.verdict == "ok")
