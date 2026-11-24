"""`project.yaml` loader (plan §3.1): the one place a project is described.

    python -m trackmanager.config project.yaml --check        validate, print the derived worker env
    python -m trackmanager.config project.yaml --get controller.image

`load()` builds dataclasses from the YAML, rejects unknown keys (reporting the dotted path), and
`ProjectConfig.validate()` collects every problem into one `ConfigError`. Relative paths are
taken from the project root. The controller re-reads only `limits()` each cycle, so a typo in
a live edit keeps the previous limits instead of stopping the project.
"""
from __future__ import annotations

import argparse
import dataclasses as dc
import json
import os
import re
import sys
import typing
from pathlib import Path

import yaml

from . import states

REPO = Path(__file__).resolve().parent.parent
EB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ConfigError(ValueError):
    pass


def _f(default=None, **kw):
    return dc.field(default=default, **kw)


def _d():
    return dc.field(default_factory=dict)


@dc.dataclass
class ProjectCfg:
    name: str = ""
    vla_project_codes: list = dc.field(default_factory=list)
    root: str | None = None
    group: str | None = None            # operators' CADC group (documentation; ACLs are set by hand)
    session_prefix: str = "tm"          # prefix of every session this project submits; keep unique per space


@dc.dataclass
class InputsCfg:
    source: str = "arc_path"            # nrao_link | arc_path
    eb_list: str = "archive"            # archive | file:<csv>
    arc_dir: str | None = None          # arc_path + archive tracks: <arc_dir>/<SDM> (directory) or <SDM>.tar


@dc.dataclass
class ArchiveCfg:                       # used from M2 on
    tap_url: str = "https://data-query.nrao.edu/tap"
    poll_h: float = 24
    bands_ghz: list = dc.field(default_factory=lambda: [[1.0, 2.0]])
    cones: list = dc.field(default_factory=list)
    include_ebs: list = dc.field(default_factory=list)
    exclude_ebs: list = dc.field(default_factory=list)
    min_duration_min: float = 30
    size_calibration: float = 1.0


@dc.dataclass
class WorkerCfg:
    cmd: str = "bash"
    args: str = str(REPO / "workers" / "calibrate_worker.sh")


@dc.dataclass
class StageWorkerCfg:
    """The `stage_local` worker (plan §3.4): a small flexible session, so it has no fixed request.
    `image` defaults to controller.image, which has python 3 and tar."""
    image: str | None = None
    cmd: str = "python3"
    args: str = "trackmanager/stage_worker.py"
    size_tolerance: float = 0.05        # |bytes / TAP estimate - 1| allowed before a stage fails


@dc.dataclass
class ResourceCfg:
    cores: int | None = 8
    ram: int | None = 64                # GB


@dc.dataclass
class ResourcesCfg:
    default: ResourceCfg = dc.field(default_factory=ResourceCfg)
    by_config: dict = dc.field(default_factory=dict)   # array config (A, B, C, D) -> {cores, ram}


@dc.dataclass
class PipelineCfg:
    image: str = ""
    script: str = "lband_pipeline/lband_reduction_pipeline.py"
    casa_passes: int = 2
    repo: str | None = str(REPO.parent / "ReductionPipeline")   # ReductionPipeline checkout the worker runs
    source_configs: str | None = None   # master_config.cfg + calibrator/target cfgs -> VLA_PIPELINE_CONFIG_DIR
    casa_home: str | None = None        # CASA install: bin/casa runs the pipeline, bin/python3 runs QAPlotter
    qa_deps: str | None = None          # QAPlotter + deps for CASA's python (qa/install_qadeps.sh)
    antcorr_tables: str | None = None   # offline fallback only
    sdm_mode: str = "link"              # link | copy
    workdir: str = "arc"                # arc | scratch | auto
    scratch_max_sdm_gib: float = 20
    disk_factor: float = 8.0
    resources: ResourcesCfg = dc.field(default_factory=ResourcesCfg)
    walltime_h: float = 168
    worker: WorkerCfg = dc.field(default_factory=WorkerCfg)
    stage_worker: StageWorkerCfg = dc.field(default_factory=StageWorkerCfg)
    env: dict = _d()                    # extra worker environment, for every track


@dc.dataclass
class QaCfg:
    products_dir: str = "products"      # outside tracks/, so a reviewer's link never changes
    hosting: str = "local"
    group: str | None = None            # read-only CADC reviewer group
    url_base: str = "https://ws-uv.canfar.net/arc/files"


@dc.dataclass
class ReviewCfg:
    enabled: bool = True                # false: a verified run goes straight to PROMOTING
    stale_after_h: float = 168
    escalate_after_iterations: int = 6
    email_prefix: str = "[QA]"


@dc.dataclass
class SheetsCfg:                        # used from M4 on (plan §3.5)
    enabled: bool = False
    tracker_id: str | None = None       # spreadsheet ids (the part of the URL after /d/)
    flagbook_id: str | None = None      # the template workbook: README + the project's flag TEMPLATE
    flags_folder_id: str | None = None  # Drive folder holding one flag spreadsheet per track
    credentials: str | None = None      # service-account JSON key, mode 600
    editors: list | None = None         # human editors of protected ranges; unset = sheets.DEFAULT_EDITORS
    links: dict = _d()                  # qa_guide, flag_rows_help (URLs shown in the sheets and emails)
    protection_check_cycles: int = 12   # read the protections back every N cycles (and at start)


@dc.dataclass
class ControllerCfg:
    image: str = "images.canfar.net/skaha/base-notebook:latest"
    poll_s: float = 300
    grace_s: float = 900
    relaunch_after_d: float | None = None   # days; fractions allowed (tests use seconds / 86400)
    max_workers: dict = dc.field(default_factory=lambda: {"calibrate": 1})   # per worker stage; 0 holds the stage
    max_sessions: int | None = None     # shared cap on live worker sessions across stages
    quota_tb: float | None = None       # project space allocation; None turns the disk gate off
    min_free_tb: float | None = None    # the gate keeps this much of quota_tb unreserved
    auto_retry: int = 1
    keep_running: bool = False          # stay up when every track has settled
    cancel_successor_on_finish: bool = True
    successor_env: dict = _d()


@dc.dataclass
class RetentionCfg:
    trim_work: bool = True
    drop_prev_iter_work: bool = True
    keep_raw_after_done: bool = False
    science_dir: str = "science"


@dc.dataclass
class TrackCfg:
    """A track seeded from the config (M2's archive sync replaces this). `mode` and `sleep_s`
    drive the placeholder worker in tests."""
    eb: str = ""
    priority: int = 0
    config: str | None = None           # array config, selects pipeline.resources.by_config
    path: str | None = None             # SDM directory or NRAO tar, staged by stage_local
    size_gb: float | None = None        # SDM size when there is no TAP estimate (disk gate)
    env: dict = _d()
    mode: str | None = None             # placeholder-worker fault injection (tests)
    sleep_s: float | None = None
    stage_mode: str | None = None       # the same for the stage worker
    stage_sleep_s: float | None = None

    def spec(self) -> dict:
        d = {k: v for k, v in dc.asdict(self).items() if v not in (None, {}) and k != "eb"}
        d["eb"] = self.eb
        return d


@dc.dataclass
class Limits:
    max_workers: dict
    max_sessions: int | None
    min_free_tb: float | None
    quota_tb: float | None = None
    disk_factor: float = 8.0


DEFAULT_STAGE_WORKERS = 4               # used when controller.max_workers has no `stage` entry


@dc.dataclass
class ProjectConfig:
    project: ProjectCfg = dc.field(default_factory=ProjectCfg)
    inputs: InputsCfg = dc.field(default_factory=InputsCfg)
    archive: ArchiveCfg = dc.field(default_factory=ArchiveCfg)
    pipeline: PipelineCfg = dc.field(default_factory=PipelineCfg)
    qa: QaCfg = dc.field(default_factory=QaCfg)
    review: ReviewCfg = dc.field(default_factory=ReviewCfg)
    sheets: SheetsCfg = dc.field(default_factory=SheetsCfg)
    controller: ControllerCfg = dc.field(default_factory=ControllerCfg)
    retention: RetentionCfg = dc.field(default_factory=RetentionCfg)
    tracks: list = dc.field(default_factory=list)

    # set by load(); not part of the YAML
    path: str | None = dc.field(default=None, repr=False)
    root: Path = dc.field(default=Path("."), repr=False)

    # ---- derived values ----------------------------------------------------
    @property
    def name(self) -> str:
        return self.project.name

    def resolve(self, p) -> Path:
        p = Path(os.path.expanduser(str(p)))
        return p if p.is_absolute() else self.root / p

    @property
    def products_dir(self) -> Path:
        return self.resolve(self.qa.products_dir)

    @property
    def science_dir(self) -> Path:
        return self.resolve(self.retention.science_dir)

    @property
    def sheets_key(self) -> Path | None:
        return self.resolve(self.sheets.credentials) if self.sheets.credentials else None

    def track_specs(self) -> list[dict]:
        return [t.spec() for t in self.tracks]

    def stage_worker_args(self) -> str:
        a = Path(self.pipeline.stage_worker.args)
        return str(a if a.is_absolute() else REPO / a)

    def stage_image(self) -> str:
        return self.pipeline.stage_worker.image or self.controller.image

    def input_path(self, spec: dict, sdm: str | None = None) -> Path | None:
        """Where stage_local finds this track's SDM or tar: the track's own `path`, else
        <inputs.arc_dir>/<sdm> (a directory) or <sdm>.tar. None if neither is configured."""
        if spec.get("path"):
            return self.resolve(spec["path"])
        sdm = sdm or spec.get("sdm")
        if not (self.inputs.arc_dir and sdm):
            return None
        base = self.resolve(self.inputs.arc_dir)
        tar = base / f"{sdm}.tar"
        return base / sdm if (base / sdm).exists() or not tar.exists() else tar

    def worker_args(self) -> str:
        """The worker script; a relative path is taken from the repo (e.g. trackmanager/worker.py)."""
        a = Path(self.pipeline.worker.args)
        return str(a if a.is_absolute() else REPO / a)

    def resources(self, spec: dict | None = None) -> ResourceCfg:
        res = self.pipeline.resources
        by = res.by_config.get((spec or {}).get("config"))
        return by if isinstance(by, ResourceCfg) else res.default

    def worker_env(self, spec: dict | None = None) -> dict:
        """The environment a worker needs, derived from the config. A real calibrate worker needs
        the CASA/QA paths; a placeholder worker (no casa_home) gets none of them. Precedence:
        derived < pipeline.env < the track's own env."""
        spec, p, env = spec or {}, self.pipeline, {}
        if p.casa_home:
            env.update(CASA_HOME=str(self.resolve(p.casa_home)),
                       CASA_WRAPPER=str(REPO / "casa" / "casa_with_limits.sh"),
                       QA_SCRIPT=str(REPO / "qa" / "run_qa.sh"), SDM_MODE=p.sdm_mode)
            if p.repo:
                env["PIPELINE"] = str(self.resolve(p.repo))
        if p.qa_deps:
            env["QA_DEPS"] = str(self.resolve(p.qa_deps))
        if p.source_configs:
            env["VLA_PIPELINE_CONFIG_DIR"] = str(self.resolve(p.source_configs))
        if p.antcorr_tables:
            env["ANTCORR_DIR"] = str(self.resolve(p.antcorr_tables))
        if self.resources(spec).cores:
            env["OMP_NUM_THREADS"] = str(self.resources(spec).cores)
        env.update({k: str(v) for k, v in {**p.env, **(spec.get("env") or {})}.items()})
        return env

    def limits(self) -> Limits:
        c = self.controller
        return Limits({"stage": DEFAULT_STAGE_WORKERS, **c.max_workers}, c.max_sessions, c.min_free_tb,
                      c.quota_tb, self.pipeline.disk_factor)

    def reload_limits(self) -> Limits:
        """Re-read the limits from the file this config came from; keep the current ones if the
        file can't be read or no longer validates."""
        if not self.path:
            return self.limits()
        try:
            fresh = load(self.path, root=self.root, check_paths=False)
        except (OSError, ValueError, yaml.YAMLError) as e:
            print(f"config re-read failed, keeping the previous limits: {e!r}", flush=True)
            return self.limits()
        self.controller.max_workers = fresh.controller.max_workers
        self.controller.max_sessions = fresh.controller.max_sessions
        self.controller.min_free_tb = fresh.controller.min_free_tb
        self.controller.quota_tb = fresh.controller.quota_tb
        self.pipeline.disk_factor = fresh.pipeline.disk_factor
        return self.limits()

    # ---- validation --------------------------------------------------------
    def validate(self, check_paths: bool = True) -> None:
        errs: list[str] = []

        def need(ok, msg):
            if not ok:
                errs.append(msg)

        need(self.project.name, "project.name is required")
        need(self.project.root or str(self.root) != ".", "project.root is required (or pass --root)")
        need(re.fullmatch(r"[A-Za-z0-9]+", self.project.session_prefix or ""),
             "project.session_prefix must be letters and digits")
        need(self.inputs.source in ("nrao_link", "arc_path"), "inputs.source must be nrao_link or arc_path")
        need(self.inputs.eb_list == "archive" or self.inputs.eb_list.startswith("file:"),
             "inputs.eb_list must be 'archive' or 'file:<csv>'")
        need(self.pipeline.workdir in ("arc", "scratch", "auto"), "pipeline.workdir must be arc, scratch or auto")
        need(self.pipeline.sdm_mode in ("link", "copy"), "pipeline.sdm_mode must be link or copy")
        need(self.pipeline.disk_factor > 0, "pipeline.disk_factor must be positive")
        need(isinstance(self.pipeline.image, str), "pipeline.image must be a string")
        res = self.pipeline.resources
        need(isinstance(res.default, ResourceCfg), "pipeline.resources.default must be {cores, ram}")
        for cfg_name, r in res.by_config.items():
            need(isinstance(r, ResourceCfg), f"pipeline.resources.by_config.{cfg_name} must be {{cores, ram}}")
        c = self.controller
        need(c.poll_s > 0 and c.grace_s >= 0, "controller.poll_s must be positive, grace_s non-negative")
        need(c.relaunch_after_d is None or c.relaunch_after_d > 0, "controller.relaunch_after_d must be positive")
        need(c.auto_retry >= 0, "controller.auto_retry must be >= 0")
        need(c.quota_tb is None or c.quota_tb > 0, "controller.quota_tb must be positive")
        need(c.min_free_tb is None or c.min_free_tb >= 0, "controller.min_free_tb must be >= 0")
        need(c.quota_tb is None or c.min_free_tb is None or c.min_free_tb < c.quota_tb,
             "controller.min_free_tb must be smaller than controller.quota_tb")
        need(0 <= self.pipeline.stage_worker.size_tolerance < 1,
             "pipeline.stage_worker.size_tolerance must be in [0, 1)")
        need(isinstance(c.max_workers, dict) and c.max_workers, "controller.max_workers must be a mapping")
        for kind, n in (c.max_workers or {}).items():
            need(kind in states.WORKER_KINDS, f"controller.max_workers.{kind}: unknown worker stage "
                 f"(known: {', '.join(states.WORKER_KINDS)})")
            need(isinstance(n, int) and not isinstance(n, bool) and n >= 0,
                 f"controller.max_workers.{kind} must be an integer >= 0")
        need(c.max_sessions is None or (isinstance(c.max_sessions, int) and c.max_sessions >= 0),
             "controller.max_sessions must be an integer >= 0")
        sh = self.sheets
        if sh.enabled:
            for key in ("tracker_id", "flagbook_id", "flags_folder_id", "credentials"):
                need(getattr(sh, key), f"sheets.{key} is required when sheets.enabled is true")
            need(not (sh.tracker_id and sh.tracker_id == sh.flagbook_id),
                 "sheets.tracker_id and sheets.flagbook_id must be different spreadsheets")
            for key in ("qa_guide", "flag_rows_help"):
                need((sh.links or {}).get(key), f"sheets.links.{key} is required when sheets.enabled is true")
        need(sh.editors is None or (isinstance(sh.editors, list)
                                    and all(isinstance(e, str) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)
                                            for e in sh.editors)),
             "sheets.editors must be a list of email addresses")
        need(sh.protection_check_cycles >= 1, "sheets.protection_check_cycles must be >= 1")
        seen = set()
        for t in self.tracks:
            need(isinstance(t, TrackCfg) and EB_NAME.fullmatch(t.eb or ""), f"tracks: invalid EB name {t!r}")
            need(getattr(t, "eb", None) not in seen, f"tracks: duplicate EB {getattr(t, 'eb', None)!r}")
            seen.add(getattr(t, "eb", None))
            if isinstance(t, TrackCfg) and t.config is not None:
                need(isinstance(t.config, str), f"tracks.{t.eb}.config must be a string")
        if check_paths:
            p = self.pipeline
            if p.casa_home:
                need(os.access(self.resolve(p.casa_home) / "bin" / "casa", os.X_OK),
                     f"pipeline.casa_home {p.casa_home} has no executable bin/casa")
            if p.source_configs:
                need(self.resolve(p.source_configs).is_dir(),
                     f"pipeline.source_configs {p.source_configs} is not a directory")
            if p.casa_home and p.repo:
                need(self.resolve(p.repo).is_dir(), f"pipeline.repo {p.repo} is not a directory")
            if sh.enabled and sh.credentials:
                from .gsheets import KeyFileError, check_key
                try:
                    check_key(self.sheets_key)
                except KeyFileError as e:
                    errs.append(f"sheets.credentials: {e}")
        if errs:
            raise ConfigError("invalid project config:\n  - " + "\n  - ".join(errs))


# ---- loading ---------------------------------------------------------------
def _hints(cls):
    return typing.get_type_hints(cls, globalns={**globals(), "typing": typing})


def _build(cls, data, where):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'project'}: expected a mapping, got {type(data).__name__}")
    hints = _hints(cls)
    hidden = ("path", "root") if cls is ProjectConfig else ()   # set by load(), not part of the YAML
    known = [f.name for f in dc.fields(cls) if f.name not in hidden]
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"{where + '.' if where else ''}{unknown[0]}: unknown key "
                          f"(known: {', '.join(known)})")
    kw = {}
    for name, value in data.items():
        hint, here = hints[name], f"{where + '.' if where else ''}{name}"
        if dc.is_dataclass(hint):
            value = _build(hint, value, here)
        elif cls is ResourcesCfg and name == "by_config":
            value = {str(k): _build(ResourceCfg, v, f"{here}.{k}") for k, v in (value or {}).items()}
        elif cls is ProjectConfig and name == "tracks":
            if not isinstance(value, list):
                raise ConfigError("tracks: expected a list")
            value = [_build(TrackCfg, v, f"tracks[{i}]") for i, v in enumerate(value)]
        kw[name] = value
    return cls(**kw)


def load(path, root=None, check_paths: bool = True) -> ProjectConfig:
    """Load and validate a project.yaml. `root` (the --root option) overrides project.root."""
    path = Path(path).resolve()
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: not valid YAML: {e}") from e
    cfg = _build(ProjectConfig, data, "")
    r = root or cfg.project.root
    cfg.path = str(path)
    cfg.root = Path(os.path.expanduser(str(r))).resolve() if r else Path(".")
    cfg.validate(check_paths=check_paths)
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project")
    p.add_argument("--root", help="override project.root")
    p.add_argument("--check", action="store_true", help="validate (paths included) and print the derived worker env")
    p.add_argument("--get", metavar="KEY", help="print one value, e.g. controller.image")
    a = p.parse_args(argv)
    try:
        cfg = load(a.project, root=a.root, check_paths=a.check)
    except ConfigError as e:
        sys.exit(str(e))
    if a.get:
        val = dc.asdict(cfg)
        for part in a.get.split("."):
            val = val[part]
        print(val if isinstance(val, str) else json.dumps(val))
    elif a.check:
        print(f"{cfg.project.name}: OK (root {cfg.root})")
        for t in cfg.track_specs() or [{}]:
            print(f"worker env for {t.get('eb', '<no tracks>')}:")
            print(json.dumps(cfg.worker_env(t), indent=1, sort_keys=True))
    else:
        print(json.dumps(dc.asdict(cfg), indent=1, default=str))


if __name__ == "__main__":
    main()
