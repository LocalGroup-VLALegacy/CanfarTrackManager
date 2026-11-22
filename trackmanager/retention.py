"""Retention rules for the track database (plan §3.2): what is deleted after a verified run, on a
restart, and when a track is promoted. Each function takes the controller (`ctl`)."""
from __future__ import annotations

import shutil
from pathlib import Path

from . import qa
from .store import read_json


def drop_previous_work(ctl, eb, prev):
    """retention.drop_prev_iter_work: a restart replaces iteration <prev>, so its splits/ and
    any work/ go; its products/, inputs/, logs/ and review.json stay as the record."""
    if not ctl.retention("drop_prev_iter_work"):
        return
    idir, res = ctl.r.iter_dir(eb, prev), read_json(ctl.r.result(eb, prev))
    if not (res and res.get("ok") and Path(res.get("qa_dir") or qa.qa_dir(ctl, eb, prev)).is_dir()):
        return
    gone = [d.name for d in intermediate_dirs(idir) + [idir / "splits"] if d.exists()]
    for name in gone:
        shutil.rmtree(idir / name, ignore_errors=True)
    if gone:
        ctl.event(eb, "retention", "dropped", f"iter_{prev}: deleted {', '.join(gone)} "
                   "(replaced by a restart; products kept)")


def intermediate_dirs(idir):
    """work/ plus work and products dirs that failed attempts left aside (*.stale.*)."""
    return [idir / "work"] + sorted(idir.glob("work.stale.*")) + sorted(idir.glob("products.stale.*"))


def trim_work(ctl, eb, n):
    """retention.trim_work, after a verified run. The worker trims too; this catches a worker
    that was killed after writing result.json and covers older workers."""
    if not ctl.retention("trim_work"):
        return
    idir = ctl.r.iter_dir(eb, n)
    gone = [d.name for d in intermediate_dirs(idir) if d.exists()]
    for name in gone:
        shutil.rmtree(idir / name, ignore_errors=True)
    if gone:
        ctl.event(eb, "retention", "trimmed", f"iter_{n}: deleted {', '.join(gone)}")
