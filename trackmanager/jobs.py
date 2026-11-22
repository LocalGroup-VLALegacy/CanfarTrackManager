"""Project job log: every session the controller submitted, one row per job.

The controller appends a row to jobs/jobs.jsonl whenever a job is submitted or changes status
(plan §3.2; append-only, so it is the full history). This module folds that history into one
row per job and, every controller cycle, rewrites three views of it:

  jobs/summary.json   machine-readable, keyed "<eb>#i<iteration>a<attempt>" (or the session
                      name for controller successors)
  jobs/jobs.csv       the same table for a spreadsheet
  jobs/jobs.log       a fixed-width text table, newest first, for a terminal or a quick look

Columns: job id (CANFAR session id), session name, kind (calibrate | stage | controller), track (EB),
iteration, attempt, status (last known), outcome (verified | failure class), submitted,
started, ended, wall time, cores/RAM requested, peak memory and workdir, exit code, the review
verdict on that job's output (pass | restart | manual_review | help_requested) and, for the job
whose output was approved, when it was promoted. So "which job made the data we kept" is the
row with a promoted time.

Track view (state/tracks.csv, state/tracks.log): one row per track with its stage, iteration,
job count, failed-job count, and the completion record from state/<EB>.json (completed_at,
iteration and attempt approved, reviewer, science_dir, products_dir, raw_deleted_at).

Query from a shell:
    python -m trackmanager.jobs --root <run root> [--eb EB] [--status failed] [--kind calibrate] [--csv]
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

from .store import Root, read_jsonl, write_json

COLUMNS = ("job_id", "name", "kind", "eb", "iteration", "attempt", "status", "outcome",
           "submitted", "started", "ended", "wall_h", "cores", "ram", "peak_rss_gb", "peak_disk_gb",
           "exit_code", "platform_status", "verdict", "verdict_at", "promoted", "log_dir")
DONE_STATUSES = ("verified", "failed_verification")


def job_key(row: dict) -> str:
    if row.get("kind") == "controller":
        return row["name"]
    if row.get("kind") == "stage":
        return f"{row['eb']}#stage-a{row['attempt']}"
    return f"{row['eb']}#i{row.get('iteration', 1)}a{row['attempt']}"


def fold(rows: list[dict]) -> dict[str, dict]:
    """One dict per job: latest non-null value of each field, plus first/last timestamps."""
    jobs: dict[str, dict] = {}
    for row in rows:
        key = job_key(row)
        j = jobs.setdefault(key, {"submitted": row.get("ts")})
        j.update({k: v for k, v in row.items() if v is not None})
        status = row.get("status")
        if status == "Running" and not j.get("started"):
            j["started"] = row.get("ts")
        if status in DONE_STATUSES:
            j["ended"] = row.get("ts")
    for j in jobs.values():
        j.setdefault("kind", "calibrate")
        if j.get("status") == "verified":
            j["outcome"] = "verified"
        elif j.get("status") == "failed_verification":
            j["outcome"] = j.get("failure_class") or "failed"
        if j.get("wall_s") is not None:
            j["wall_h"] = round(float(j["wall_s"]) / 3600, 2)
        if j.get("peak_rss_mb") is not None:
            j["peak_rss_gb"] = round(float(j["peak_rss_mb"]) / 1024, 1)
    return jobs


def table(jobs: dict[str, dict]) -> list[dict]:
    """Rows newest first, restricted to COLUMNS."""
    rows = [{c: j.get(c, "") for c in COLUMNS} for j in jobs.values()]
    return sorted(rows, key=lambda r: r["submitted"] or "", reverse=True)


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


TEXT_COLS = (("submitted", 21), ("job_id", 10), ("kind", 11), ("eb", 0), ("iteration", 5),
             ("attempt", 4), ("status", 20), ("outcome", 10), ("wall_h", 7), ("peak_rss_gb", 7),
             ("peak_disk_gb", 8), ("ended", 21), ("verdict", 15), ("promoted", 21))
TEXT_HEAD = {"iteration": "iter", "attempt": "att", "status": "last status", "peak_rss_gb": "rss_GB",
             "peak_disk_gb": "disk_GB", "eb": "track"}


def to_text(rows: list[dict], title: str = "") -> str:
    ebw = max([8] + [len(str(r["eb"])) + 2 for r in rows])
    widths = {c: (w or ebw) for c, w in TEXT_COLS}
    line = lambda vals: "".join(f"{str(vals[c]):<{widths[c]}}" for c, _ in TEXT_COLS).rstrip()
    out = [title] if title else []
    out.append(line({c: TEXT_HEAD.get(c, c) for c, _ in TEXT_COLS}))
    out += [line(r) for r in rows]
    return "\n".join(out) + "\n"


def write_views(root: Root, project: str = "", ts: str = "") -> dict[str, dict]:
    """Rewrite summary.json, jobs.csv and jobs.log from jobs.jsonl (atomically)."""
    jobs = fold(read_jsonl(root.jobs))
    write_json(root.jobs_summary, jobs)
    rows = table(jobs)
    for name, text in (("jobs.csv", to_csv(rows)),
                       ("jobs.log", to_text(rows, f"# jobs for project {project or root.path.name}"
                                                  f" ({len(rows)} jobs; updated {ts})"))):
        path = root.jobs.parent / name
        tmp = path.with_name(f".{name}.tmp")
        tmp.write_text(text)
        tmp.replace(path)
    return jobs


TRACK_COLUMNS = ("eb", "stage", "iteration", "attempt", "hold", "jobs", "failed_jobs", "wall_h_total",
                 "completed_at", "completed_iteration", "completed_attempt", "completed_by",
                 "science_dir", "products_dir", "raw_deleted_at", "updated", "detail")


def track_rows(states: dict, jobs: dict) -> list[dict]:
    rows = []
    for eb, st in sorted(states.items()):
        mine = [j for j in jobs.values() if j.get("eb") == eb and j.get("kind") != "controller"]
        rows.append({**{c: st.get(c, "") for c in TRACK_COLUMNS},
                     "iteration": st.get("iteration", 1), "jobs": len(mine),
                     "failed_jobs": sum(j.get("status") == "failed_verification" for j in mine),
                     "wall_h_total": round(sum(float(j.get("wall_s") or 0) for j in mine) / 3600, 2)})
    return rows


def write_track_views(root: Root, states: dict, jobs: dict, project: str = "", ts: str = "") -> None:
    rows = track_rows(states, jobs)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TRACK_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    ebw = max([8] + [len(r["eb"]) + 2 for r in rows])
    cols = (("eb", ebw), ("stage", 18), ("iteration", 5), ("jobs", 5), ("failed_jobs", 7),
            ("wall_h_total", 8), ("completed_at", 21), ("completed_iteration", 6), ("raw_deleted_at", 21))
    head = {"iteration": "iter", "failed_jobs": "failed", "wall_h_total": "wall_h",
            "completed_iteration": "c.it", "raw_deleted_at": "raw deleted", "eb": "track"}
    line = lambda r: "".join(f"{str(r[c]):<{n}}" for c, n in cols).rstrip()
    text = "\n".join([f"# tracks for project {project or root.path.name} ({len(rows)}; updated {ts})",
                      line({c: head.get(c, c) for c, _ in cols})] + [line(r) for r in rows]) + "\n"
    for name, content in (("tracks.csv", buf.getvalue()), ("tracks.log", text)):
        path = root.summary.parent / name
        tmp = path.with_name(f".{name}.tmp")
        tmp.write_text(content)
        tmp.replace(path)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True)
    p.add_argument("--eb", help="only this track")
    p.add_argument("--status", help="match last status or outcome (substring, e.g. failed, oom, verified)")
    p.add_argument("--kind", help="calibrate | controller")
    p.add_argument("--csv", action="store_true", help="CSV instead of the text table")
    a = p.parse_args(argv)
    rows = table(fold(read_jsonl(Root(Path(a.root)).jobs)))
    if a.eb:
        rows = [r for r in rows if r["eb"] == a.eb]
    if a.kind:
        rows = [r for r in rows if r["kind"] == a.kind]
    if a.status:
        rows = [r for r in rows if a.status in str(r["status"]) or a.status in str(r["outcome"])]
    sys.stdout.write(to_csv(rows) if a.csv else to_text(rows))


if __name__ == "__main__":
    main()
