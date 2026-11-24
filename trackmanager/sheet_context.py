"""What the Sheets bridge shows for one track (plan §3.5): the spec's column sources
(archive.*, state.*, job.*, run.*, review.*, links.*) filled from the track database.

    ctx = track_context(r, cfg, st, jobs, bridge_state)

`archive` is also what flagcmds checks a reviewer's rows against, so it carries the scan
numbers (archive_scans.csv), the SPW count (TAP) and the antenna names (the staged SDM's
Antenna.xml, via stage/manifest.json). A track with no archive record (a config track) gets
what its manifest and spec give; checks that need missing metadata are skipped by flagcmds.
"""
from __future__ import annotations

import csv
import datetime as dt

from . import qa
from .store import read_json

MJD0 = dt.datetime(1858, 11, 17)


def mjd_to_utc(mjd) -> dt.datetime | None:
    try:
        return MJD0 + dt.timedelta(days=float(mjd))
    except (TypeError, ValueError):
        return None


def _stamp(t: dt.datetime | None) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else ""


def iso_to_sheet(ts: str | None) -> str:
    """2026-11-24T03:01:29Z -> 2026-11-24 03:01:29 (UTC; the sheets are set to UTC)."""
    return (ts or "").replace("T", " ").replace("Z", "").split("+")[0]


def scans(r, eb) -> list[int]:
    try:
        with open(r.archive_scans(eb), newline="") as f:
            return sorted({int(float(row["scan_num"])) for row in csv.DictReader(f) if row.get("scan_num")})
    except (OSError, KeyError, ValueError):
        return []


def archive_ctx(r, st, spec: dict | None = None) -> dict:
    """spec: the track's spec (config tracks keep it in project.yaml: Controller.spec(st))."""
    eb = st["eb"]
    rec = read_json(r.archive_json(eb)) or {}
    man = read_json(r.manifest(eb)) or {}
    spec = spec or st.get("spec") or {}
    start, end = mjd_to_utc(rec.get("start")), mjd_to_utc(rec.get("end"))
    targets = (st.get("preflight") or {}).get("targets") or []
    fields = rec.get("fields") or sorted(man.get("fields") or {})
    out = {"eb": eb, "sdm": rec.get("sdm") or spec.get("sdm") or man.get("sdm") or eb,
           "ebid": rec.get("ebid"), "sbid": rec.get("sbid"), "mjd": rec.get("mjd"),
           "project": rec.get("project_code") or "",
           "date": start.strftime("%Y-%m-%d") if start else "",
           "start": _stamp(start), "end": _stamp(end),
           "duration_min": round(rec["duration_min"], 1) if rec.get("duration_min") is not None else None,
           "config": rec.get("config") or spec.get("config") or "",
           "target": ", ".join(targets) or spec.get("target") or "",
           "fields": fields,
           "on_source_min": round(rec["on_source_s"] / 60, 1) if rec.get("on_source_s") is not None else None,
           "est_size_gb": rec.get("est_size_gb") if rec else spec.get("size_gb"),
           "archive_status": rec.get("archive_status", ""), "archive_url": rec.get("archive_url", ""),
           "scans": scans(r, eb), "n_spw": rec.get("n_spw"), "antennas": man.get("antennas") or []}
    return out


def latest_job(jobs: dict, eb: str) -> dict:
    mine = [j for j in jobs.values() if j.get("eb") == eb and j.get("kind", "calibrate") == "calibrate"]
    return max(mine, key=lambda j: j.get("submitted") or "", default={})


def last_verdict(r, st) -> str:
    """The most recent verdict on this track, e.g. "RESTART (iteration 1)"."""
    for n in range(st.get("iteration", 1), 0, -1):
        rv = read_json(r.review(st["eb"], n))
        if rv and rv.get("verdict"):
            word = rv.get("verdict_word") or rv["verdict"].replace("_", " ").upper()
            return f"{word} (iteration {n})"
    return ""


def detail(st) -> str:
    text = st.get("detail") or ""
    if st.get("hold"):
        why = (st.get("hold_reason") or "operator").replace("_", " ")
        return f"HOLD: {why}" + (f" -- {text}" if text else "")
    return text


def track_context(r, cfg, st, jobs: dict, bridge: dict, spec: dict | None = None) -> dict:
    """bridge: the bridge's own state for this track ({signup, ack, tab_url, completed})."""
    eb, n = st["eb"], st.get("iteration", 1)
    job = latest_job(jobs, eb)
    man = read_json(r.manifest(eb)) or {}
    refant = r.inputs(eb, n) / "refant_ignore.txt"
    hist = cfg.products_dir / eb / "index.html"
    signup = bridge.get("signup") or {}
    return {
        "archive": archive_ctx(r, st, spec),
        "state": {"stage": st["stage"], "iteration": n, "detail": detail(st),
                  "updated": iso_to_sheet(st.get("updated"))},
        "job": {"job_id": job.get("job_id"), "wall_h": job.get("wall_h"), "peak_disk_gb": job.get("peak_disk_gb")},
        "run": {"sdm_gb": round(man["bytes"] / 1e9, 2) if man.get("bytes") else None},
        "review": {"reviewer": signup.get("reviewer", ""), "signed_up": signup.get("signed_up", ""),
                   "verdict": last_verdict(r, st),
                   "refant_ignore": refant.read_text().strip() if refant.exists() else "",
                   "ack": bridge.get("ack", ""), "completed": "COMPLETED" if st["stage"] == "DONE" else ""},
        "links": {"qa": st.get("qa_url") or "",
                  "qa_track": (qa.url_for(cfg.qa.url_base, hist) or "") if hist.exists() else "",
                  "flag_tab": bridge.get("tab_url", "")},
    }
