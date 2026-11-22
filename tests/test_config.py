"""project.yaml loading (trackmanager/config.py)."""
from pathlib import Path

import pytest
import yaml

from trackmanager import config as C

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((REPO / "examples").glob("project_*.yaml"))
TEMPLATE = REPO / "trackmanager" / "templates" / "project.yaml"

# What examples/project_ic10.json spelled out by hand (worker.env) plus what the controller
# added from its own settings, before M1. The YAML must derive exactly this.
LEGACY_IC10_ENV = {
    "TAR": "/arc/projects/VVINGS/ic10_d_testcase/ic10_d_sdm.tar",
    "PIPELINE": "/arc/home/ekoch/vla_processing/ReductionPipeline",
    "CASA_WRAPPER": "/arc/home/ekoch/vla_processing/CanfarTrackManager/casa/casa_with_limits.sh",
    "SDM_MODE": "link",
    "QA_DEPS": "/arc/projects/VVINGS/shared/qadeps/casa-6.6.6-18-py3.10",
    "QA_SCRIPT": "/arc/home/ekoch/vla_processing/CanfarTrackManager/qa/run_qa.sh",
    "VLA_PIPELINE_CONFIG_DIR": "/arc/projects/VVINGS/vla-calib-mwe/config/ic10/pipeline",
    "CASA_HOME": "/arc/home/ekoch/casa-6.6.6-18-pipeline-2025.1.0.37-py3.10.el8",
    "OMP_NUM_THREADS": "8",
}


def write(tmp_path, data, name="project.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return p


def minimal(**over):
    d = {"project": {"name": "t", "root": "run"}, "tracks": [{"eb": "a"}]}
    d.update(over)
    return d


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_load_without_path_checks(path):
    cfg = C.load(path, check_paths=False)
    assert cfg.name and cfg.tracks
    assert cfg.limits().max_workers


def test_starter_template_has_every_block_and_loads_with_placeholders_filled(tmp_path):
    data = yaml.safe_load(TEMPLATE.read_text())
    assert set(data) == {"project", "inputs", "archive", "pipeline", "qa", "review", "sheets",
                         "controller", "retention", "tracks"}
    data["project"]["root"] = str(tmp_path / "run")
    data["pipeline"]["casa_home"] = None
    data["pipeline"]["source_configs"] = None
    cfg = C.load(write(tmp_path, data), check_paths=False)
    assert cfg.controller.max_workers == {"calibrate": 20}
    assert cfg.products_dir == tmp_path / "run" / "calibrated" / "products"


def test_ic10_example_derives_the_old_worker_env():
    cfg = C.load(REPO / "examples" / "project_ic10.yaml", check_paths=False)
    (spec,) = cfg.track_specs()
    assert cfg.worker_env(spec) == LEGACY_IC10_ENV
    assert cfg.resources(spec) == C.ResourceCfg(8, 64)
    assert cfg.worker_args() == str(REPO / "workers" / "calibrate_worker.sh")


def test_placeholder_worker_gets_no_casa_env(tmp_path):
    cfg = C.load(REPO / "examples" / "project_fake.yaml", check_paths=False)
    assert cfg.worker_env(cfg.track_specs()[0]) == {}
    assert cfg.worker_args() == str(REPO / "trackmanager" / "worker.py")


def test_env_precedence_and_resources_by_config(tmp_path):
    d = minimal(pipeline={"resources": {"default": {"cores": 8, "ram": 64},
                                        "by_config": {"A": {"cores": 16, "ram": 128}}},
                          "env": {"X": "pipeline", "Y": "pipeline"}},
                tracks=[{"eb": "a", "config": "A", "env": {"Y": "track"}}, {"eb": "b", "config": "D"}])
    cfg = C.load(write(tmp_path, d), check_paths=False)
    a, b = cfg.track_specs()
    assert cfg.resources(a) == C.ResourceCfg(16, 128) and cfg.resources(b) == C.ResourceCfg(8, 64)
    assert cfg.worker_env(a) == {"OMP_NUM_THREADS": "16", "X": "pipeline", "Y": "track"}
    assert cfg.worker_env(b)["OMP_NUM_THREADS"] == "8" and cfg.worker_env(b)["Y"] == "pipeline"


def test_relative_paths_resolve_from_the_root_and_cli_root_overrides(tmp_path):
    d = minimal(pipeline={"source_configs": "config/pipeline"}, qa={"products_dir": "qa"},
                retention={"science_dir": "/abs/science"})
    cfg = C.load(write(tmp_path, d), check_paths=False)
    run = tmp_path / "run"
    assert cfg.root == run.resolve() or cfg.root == Path("run").resolve()
    other = C.load(write(tmp_path, d), root=tmp_path / "other", check_paths=False)
    assert other.worker_env({})["VLA_PIPELINE_CONFIG_DIR"] == str(tmp_path / "other" / "config" / "pipeline")
    assert other.products_dir == tmp_path / "other" / "qa"
    assert other.science_dir == Path("/abs/science")


@pytest.mark.parametrize("data, message", [
    (minimal(bogus=1), "bogus: unknown key"),
    (minimal(controller={"poll": 3}), "controller.poll: unknown key"),
    (minimal(pipeline={"worker": {"cmdd": "x"}}), "pipeline.worker.cmdd: unknown key"),
    (minimal(controller={"max_workers": {"fetch": 2}}), "unknown worker stage"),
    (minimal(controller={"max_workers": {"calibrate": -1}}), "integer >= 0"),
    (minimal(controller={"max_workers": {"calibrate": True}}), "integer >= 0"),
    (minimal(inputs={"source": "ftp"}), "inputs.source"),
    (minimal(tracks=[{"eb": "a"}, {"eb": "a"}]), "duplicate EB"),
    (minimal(tracks=[{"eb": "bad name"}]), "invalid EB name"),
    (minimal(tracks="x"), "tracks: expected a list"),
    (minimal(pipeline={"workdir": "nfs"}), "pipeline.workdir"),
    (minimal(project={"root": "run"}), "project.name is required"),
])
def test_invalid_configs_name_the_problem(tmp_path, data, message):
    with pytest.raises(C.ConfigError, match=message):
        C.load(write(tmp_path, data), check_paths=False)


def test_all_problems_are_reported_together(tmp_path):
    d = minimal(inputs={"source": "ftp"}, pipeline={"workdir": "nfs"})
    with pytest.raises(C.ConfigError) as e:
        C.load(write(tmp_path, d), check_paths=False)
    assert "inputs.source" in str(e.value) and "pipeline.workdir" in str(e.value)


def test_path_checks_refuse_a_missing_casa_and_configs(tmp_path):
    d = minimal(pipeline={"casa_home": str(tmp_path / "nocasa"), "source_configs": str(tmp_path / "nocfg")})
    p = write(tmp_path, d)
    C.load(p, check_paths=False)
    with pytest.raises(C.ConfigError) as e:
        C.load(p)
    assert "bin/casa" in str(e.value) and "source_configs" in str(e.value)
    casa = tmp_path / "nocasa" / "bin"
    casa.mkdir(parents=True)
    (casa / "casa").write_text("#!/bin/sh\n")
    (casa / "casa").chmod(0o755)
    (tmp_path / "nocfg").mkdir()
    (tmp_path / "ReductionPipeline").mkdir()
    d["pipeline"]["repo"] = str(tmp_path / "ReductionPipeline")
    C.load(write(tmp_path, d))


def test_reload_limits_picks_up_edits_and_survives_mistakes(tmp_path):
    p = write(tmp_path, minimal(controller={"max_workers": {"calibrate": 1}}))
    cfg = C.load(p, check_paths=False)
    assert cfg.limits().max_workers == {"calibrate": 1}
    write(tmp_path, minimal(controller={"max_workers": {"calibrate": 5}, "max_sessions": 7}))
    lim = cfg.reload_limits()
    assert lim.max_workers == {"calibrate": 5} and lim.max_sessions == 7
    p.write_text("controller: {max_workers: [oops\n")           # broken YAML
    assert cfg.reload_limits().max_workers == {"calibrate": 5}
    write(tmp_path, minimal(controller={"max_workers": {"nope": 1}}))   # fails validation
    assert cfg.reload_limits().max_workers == {"calibrate": 5}
    write(tmp_path, minimal(controller={"max_workers": {"calibrate": 0}}))
    assert cfg.reload_limits().max_workers == {"calibrate": 0}   # 0 is a valid hold


def test_cli_get_and_check(tmp_path, capsys):
    C.main([str(EXAMPLES[0]), "--get", "controller.image"])
    assert capsys.readouterr().out.strip()
    C.main([str(REPO / "examples" / "project_fake.yaml")])
    assert '"project"' in capsys.readouterr().out
