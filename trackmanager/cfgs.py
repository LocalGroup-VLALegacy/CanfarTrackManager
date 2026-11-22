"""The project's pipeline cfgs, read with ReductionPipeline's own readers (plan §3.7 preflight).

`read_config_files.py` in the ReductionPipeline checkout (`pipeline.repo`) imports only the
standard library, so it is loaded here by file path, without CASA and without putting the
checkout on sys.path. It reads the folder named by VLA_PIPELINE_CONFIG_DIR, which is set to
`pipeline.source_configs` for the duration of the call, exactly as the worker does.
"""
from __future__ import annotations

import configparser
import contextlib
import dataclasses as dc
import importlib.util
import os
from pathlib import Path

ENV = "VLA_PIPELINE_CONFIG_DIR"


class CfgError(RuntimeError):
    """The cfgs cannot be read; preflight cannot say anything about targets until they can."""


@dc.dataclass
class Tables:
    vsys: dict           # target name (substring of the field name) -> Vsys km/s
    vrange: dict         # target -> {line: [...]}  (protected velocity ranges)
    calibrators: dict    # calibrator (or 'A=B' alias section) -> {line: [vhigh, vlow]}
    where: str = ""      # the folder they were read from

    def calibrator_names(self) -> set:
        out = set()
        for sec in self.calibrators:
            out.add(sec)
            out.update(part.strip() for part in sec.split("="))
        return out


@contextlib.contextmanager
def _env(value):
    old = os.environ.get(ENV)
    if value:
        os.environ[ENV] = str(value)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def _module(cfg):
    path = cfg.resolve(cfg.pipeline.repo) / "lband_pipeline" / "read_config_files.py"
    if not path.is_file():
        raise CfgError(f"ReductionPipeline readers not found: {path} (pipeline.repo)")
    spec = importlib.util.spec_from_file_location("_tm_read_config_files", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load(cfg) -> Tables:
    mod = _module(cfg)
    src = cfg.resolve(cfg.pipeline.source_configs) if cfg.pipeline.source_configs else None
    try:
        with _env(src):
            return Tables(vsys=dict(mod.read_target_vsys_cfg()),
                          vrange=dict(mod.read_targets_vrange_cfg()),
                          calibrators=dict(mod.read_calibrator_absorption_cfg()),
                          where=str(mod.get_config_dir()))
    except (OSError, KeyError, ValueError, AssertionError, configparser.Error) as e:
        raise CfgError(f"cannot read the pipeline cfgs ({src or 'repo config_files'}): {e}") from e
