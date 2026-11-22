"""Shared test helpers: write a project.yaml for the fake backend from flat keyword options."""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def make_project(tmp_path, tracks, **over):
    """Write tmp_path/project.yaml (placeholder workers, local fake backend) and return
    (project path, run root). `over` takes flat options and maps them into the YAML:
    poll_s, grace_s, auto_retry, max_workers (int = calibrate limit), max_sessions,
    relaunch_after_s, keep_running, cancel_successor_on_finish, successor_env,
    review (bool), retention (dict), qa (dict); anything else is merged as a top-level block.
    """
    ctl = {"image": "local", "poll_s": over.pop("poll_s", 1), "grace_s": over.pop("grace_s", 6),
           "auto_retry": over.pop("auto_retry", 1),
           "max_workers": {"calibrate": over.pop("max_workers", 4)}}
    if "relaunch_after_s" in over:
        s = over.pop("relaunch_after_s")
        ctl["relaunch_after_d"] = s / 86400 if s else None
    for key in ("max_sessions", "keep_running", "cancel_successor_on_finish", "successor_env"):
        if key in over:
            ctl[key] = over.pop(key)
    data = {"project": {"name": "t", "root": str(tmp_path / "run")},
            "pipeline": {"image": "local", "resources": {"default": {"cores": None, "ram": None}},
                         "worker": {"cmd": sys.executable, "args": "trackmanager/worker.py"}},
            "controller": ctl, "review": {"enabled": bool(over.pop("review", False))},
            "retention": over.pop("retention", {}), "qa": over.pop("qa", {}),
            "tracks": tracks}
    data.update(over)
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(data))
    return path, tmp_path / "run"


FIXTURES = REPO / "tests" / "fixtures"


def make_archive_project(tmp_path, fixture, codes, archive=None, project=None, inputs=None, tracks=None, **over):
    """Like make_project, but tracks come from the archive: project codes, tap_url pointing at a
    fixture file (name under tests/fixtures, or an absolute path), and no config tracks."""
    path, run = make_project(tmp_path, tracks or [], **over)
    data = yaml.safe_load(path.read_text())
    fx = Path(fixture)
    fx = fx if fx.is_absolute() else FIXTURES / fx
    data["project"] = {**data["project"], "vla_project_codes": list(codes), **(project or {})}
    data["archive"] = {"tap_url": f"file://{fx}", **(archive or {})}
    data["inputs"] = {"source": "nrao_link", "eb_list": "archive", **(inputs or {})}
    path.write_text(yaml.safe_dump(data))
    return path, run
