"""init: the rerunnable setup report (plan §3.7)."""
import json
import shutil

import pytest

from helpers import FIXTURES, REPO, make_archive_project
from trackmanager import init

PIPELINE_REPO = REPO.parent / "ReductionPipeline"
pytestmark = pytest.mark.skipif(not (PIPELINE_REPO / "lband_pipeline" / "read_config_files.py").exists(),
                                reason="needs the ReductionPipeline checkout next to this repo")


def project(tmp_path, fixture="tap_synthetic.csv", codes=("99A-001",), cfg_dir=None, **kw):
    return make_archive_project(tmp_path, fixture, list(codes), **kw,
                                pipeline={"repo": str(PIPELINE_REPO), "source_configs": str(cfg_dir or FIXTURES / "pipeline_cfg")})


def run(cfg, capsys, *extra):
    code = init.main(["--project", str(cfg), "--no-path-check", *extra])
    return code, capsys.readouterr().out


def test_report_lists_what_the_operator_has_to_fix(tmp_path, capsys):
    cfg, root = project(tmp_path, max_workers=2)
    code, out = run(cfg, capsys)
    assert code == 1
    assert "5 EBs in scope" in out and "preflight_failed 1" in out and "skip_candidate 2" in out
    assert "FIELDS MISSING FROM THE CFGS" in out and "MYSTERY_1: 1 EB(s)" in out
    assert "2 skip candidate(s)" in out and "1 EB(s) excluded" in out and "bands_ghz" in out
    assert "TESTA (" in out and "NOT CLEAN" in out
    rep = json.loads(next((root / "reports").glob("init-*.json")).read_text())
    assert rep["ebs"] == 5 and rep["by_config"]["C"]["ebs"] == 3 and rep["archive_status"] == {"PUBLIC": 4, "LOCKED": 1}
    assert rep["quota"]["total_sdm_tb"] == pytest.approx((40 + 3 + 30 + 0 + 35) / 1e3, abs=1e-3)
    # the two largest EBs (40, 35 GB) x disk_factor 8.0 with 2 workers
    assert rep["quota"]["peak_active_tb"] == pytest.approx((40 + 35) * 8 / 1e3, abs=1e-3)
    assert next((root / "reports").glob("init-*.txt")).read_text().startswith("init report for t")


def test_rerun_after_a_cfg_fix_goes_clean_and_writes_no_state(tmp_path, capsys):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(FIXTURES / "pipeline_cfg", cfg_dir)
    cfg, root = project(tmp_path, cfg_dir=cfg_dir)
    assert run(cfg, capsys)[0] == 1
    (cfg_dir / "test_targets_vsys.cfg").write_text("[target_vsys_kms]\nTESTA = -100\nMYSTERY = 5\n")
    code, out = run(cfg, capsys)
    assert code == 0 and "CLEAN: no EB fails preflight" in out
    assert "FIELDS MISSING" not in out
    assert not (root / "state").exists() and (root / "tracks").exists()         # tracks start in the controller
    assert len(list((root / "reports").glob("init-*.json"))) == 2


def test_json_output_and_include_ebs_count_as_included(tmp_path, capsys):
    cfg, root = project(tmp_path, archive={"include_ebs": [2002, 2005]})
    code, out = run(cfg, capsys, "--json")
    rep = json.loads(out)
    assert rep["skip_candidates"] == {} and rep["verdicts"] == {"ok": 4, "preflight_failed": 1}


def test_unreadable_cfgs_and_a_down_archive_exit_with_a_message(tmp_path, capsys):
    cfg, _ = project(tmp_path, cfg_dir=tmp_path / "nowhere")
    with pytest.raises(SystemExit, match="pipeline cfgs"):
        init.main(["--project", str(cfg), "--no-path-check"])
    cfg, _ = project(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    import yaml
    data = yaml.safe_load(cfg.read_text())
    data["archive"]["tap_url"] = f"file://{tmp_path}/down.csv"
    cfg.write_text(yaml.safe_dump(data))
    with pytest.raises(SystemExit, match="archive sync failed"):
        init.main(["--project", str(cfg), "--no-path-check"])


def test_a_project_without_codes_says_so(tmp_path):
    from helpers import make_project
    cfg, _ = make_project(tmp_path, [])
    with pytest.raises(SystemExit, match="vla_project_codes"):
        init.main(["--project", str(cfg), "--no-path-check"])


def test_24b224_with_the_real_cfgs_is_clean(tmp_path, capsys):
    cfg, root = project(tmp_path, fixture="tap_24B-224.csv.gz", codes=("24B-224",), cfg_dir=PIPELINE_REPO / "config_files")
    code, out = run(cfg, capsys)
    rep = json.loads(next((root / "reports").glob("init-*.json")).read_text())
    assert code == 0 and rep["ebs"] == 47 and rep["targets_found"] == {"IC342": 46}
    assert sum(v["ebs"] for v in rep["by_config"].values()) == 47 and set(rep["by_config"]) == {"A", "B", "C", "D"}
    assert rep["verdicts"] == {"ok": 44, "skip_candidate": 3}
    assert 1.0 < rep["quota"]["total_sdm_tb"] < 15
