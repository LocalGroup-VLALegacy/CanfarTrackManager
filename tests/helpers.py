"""Shared test helpers: write a project.yaml for the fake backend from flat keyword options."""
import sys
import tarfile
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

REPO = Path(__file__).resolve().parent.parent


def make_project(tmp_path, tracks, **over):
    """Write tmp_path/project.yaml (placeholder workers, local fake backend) and return
    (project path, run root). `over` takes flat options and maps them into the YAML:
    poll_s, grace_s, auto_retry, max_workers (int = calibrate limit), max_workers_stage, max_sessions,
    quota_tb, min_free_tb, disk_factor,
    relaunch_after_s, keep_running, cancel_successor_on_finish, successor_env,
    review (bool), retention (dict), qa (dict); anything else is merged as a top-level block.
    """
    ctl = {"image": "local", "poll_s": over.pop("poll_s", 1), "grace_s": over.pop("grace_s", 6),
           "auto_retry": over.pop("auto_retry", 1),
           "max_workers": {"calibrate": over.pop("max_workers", 4)}}
    if "max_workers_stage" in over:
        ctl["max_workers"]["stage"] = over.pop("max_workers_stage")
    for key in ("quota_tb", "min_free_tb"):
        if key in over:
            ctl[key] = over.pop(key)
    if "relaunch_after_s" in over:
        s = over.pop("relaunch_after_s")
        ctl["relaunch_after_d"] = s / 86400 if s else None
    for key in ("max_sessions", "keep_running", "cancel_successor_on_finish", "successor_env"):
        if key in over:
            ctl[key] = over.pop(key)
    data = {"project": {"name": "t", "root": str(tmp_path / "run")},
            "pipeline": {"image": "local", "resources": {"default": {"cores": None, "ram": None}},
                         "worker": {"cmd": sys.executable, "args": "trackmanager/worker.py"},
                         "stage_worker": {"cmd": sys.executable}},
            "controller": ctl, "review": {"enabled": bool(over.pop("review", False))},
            "retention": over.pop("retention", {}), "qa": over.pop("qa", {}),
            "tracks": tracks}
    if "disk_factor" in over:
        data["pipeline"]["disk_factor"] = over.pop("disk_factor")
    data.update(over)
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(data))
    return path, tmp_path / "run"


FIXTURES = REPO / "tests" / "fixtures"


def make_archive_project(tmp_path, fixture, codes, archive=None, project=None, inputs=None, tracks=None,
                         pipeline=None, **over):
    """Like make_project, but tracks come from the archive: project codes, tap_url pointing at a
    fixture file (name under tests/fixtures, or an absolute path), and no config tracks."""
    path, run = make_project(tmp_path, tracks or [], **over)
    data = yaml.safe_load(path.read_text())
    fx = Path(fixture)
    fx = fx if fx.is_absolute() else FIXTURES / fx
    data["project"] = {**data["project"], "vla_project_codes": list(codes), **(project or {})}
    data["archive"] = {"tap_url": f"file://{fx}", **(archive or {})}
    data["inputs"] = {"source": "nrao_link", "eb_list": "archive", **(inputs or {})}
    data["pipeline"] = {**data["pipeline"], **(pipeline or {})}
    path.write_text(yaml.safe_dump(data))
    return path, run


TARGET_SCANS = [(1, "3C48", ["CALIBRATE_FLUX", "CALIBRATE_BANDPASS"]),
                (2, "J0102+5824", ["CALIBRATE_PHASE"]),
                (3, "IC10_1_CTR", ["OBSERVE_TARGET"])]


def _table(name, rows):
    body = "".join("<row>" + "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in r.items()) + "</row>\n"
                   for r in rows)
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<{name}>\n{body}</{name}>\n'


def make_fake_sdm(parent, name="20A-346.sb1.eb1.59786.37", scans=TARGET_SCANS, bdf_bytes=1000, bdf_files=None):
    """A small SDM directory: the four XML tables stage_local reads plus ASDMBinary/ with one BDF per
    scan (`bdf_files` overrides the count, to make a corrupt SDM). BDFs are sparse files of
    `bdf_bytes`, so tests can claim a large size cheaply. Returns its path."""
    sdm = Path(parent) / name
    (sdm / "ASDMBinary").mkdir(parents=True)
    fields = sorted({f for _, f, _ in scans})
    fid = {f: f"Field_{i}" for i, f in enumerate(fields)}
    (sdm / "ASDM.xml").write_text(_table("ASDM", [{"Name": "ExecBlock"}]))
    (sdm / "Field.xml").write_text(_table("FieldTable", [{"fieldId": fid[f], "fieldName": f} for f in fields]))
    (sdm / "Scan.xml").write_text(_table("ScanTable", [
        {"scanNumber": n, "numIntent": len(i), "scanIntent": f"1 {len(i)} " + " ".join(i), "sourceName": f}
        for n, f, i in scans]))
    (sdm / "Main.xml").write_text(_table("MainTable", [
        {"scanNumber": n, "subscanNumber": 1, "fieldId": fid[f]} for n, f, _ in scans]))
    for k in range(len(scans) if bdf_files is None else bdf_files):
        with open(sdm / "ASDMBinary" / f"uid____evla_bdf_{k}", "wb") as f:
            f.truncate(bdf_bytes)           # sparse: a "100 GB" SDM costs no disk
    return sdm


def make_fake_tar(path, sdm):
    """An NRAO-style tar: <project>/observation.<mjd>/<SDM>/..., which stage_local strips two levels of."""
    sdm = Path(sdm)
    with tarfile.open(path, "w") as t:
        t.add(sdm, arcname=f"20A-346/observation.59786/{sdm.name}")
    return Path(path)
