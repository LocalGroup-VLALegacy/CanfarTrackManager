"""Project setup report (plan §3.7): validate the config, sync the archive, preflight every EB.

    python -m trackmanager.init --project project.yaml [--root DIR] [--no-path-check] [--json] [--sheets]

Rerunnable. It creates <root>, runs the archive sync (tracks/<EB>/archive.json; it creates no
state files, the controller starts tracks), runs preflight on every kept EB, and writes
<root>/reports/init-<timestamp>.{txt,json}. The operator fixes the project cfgs and reruns until
the exit code is 0 (no EB with a failed preflight).

--sheets also builds the tab structure of the two spreadsheets named in project.yaml `sheets`
(trackmanager/sheets_setup.py; operator steps in docs/sheets_setup.md): tabs, headers, formulas,
validation and protection, but no rows. Rows and per-EB flag tabs are written by the controller,
so this refuses while a controller holds the lease. A project without archive discovery runs only
the sheets step.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict

from . import archive, cfgs, preflight
from . import config as projconfig
from .disk import PRODUCTS_FACTOR, SPLIT_FACTOR   # the quota model the disk gate uses (plan §3.2)
from .store import Root, age_s, iso, now, read_json, write_json

ITERATIONS = 3             # assumed iterations per track for the products estimate


def build(cfg, kept: dict, excluded: dict, results: dict, tables) -> dict:
    recs = list(kept.values())
    sizes = sorted((r["est_size_gb"] for r in recs), reverse=True)
    workers = max(1, cfg.controller.max_workers.get("calibrate", 1))
    total = sum(sizes)
    targets = Counter(t for res in results.values() for t in res.targets)
    missing = defaultdict(list)
    for eb, res in results.items():
        for f, kind in res.fields.items():
            if kind == "unclassified":
                missing[f].append(eb)
    sigs = Counter(r["spw_signature"] for r in recs)
    main = preflight.main_signature(recs)
    verdicts = Counter(res.verdict for res in results.values())
    by_config = defaultdict(list)
    for r in recs:
        by_config[r["config"] or "?"].append(r["est_size_gb"])
    return {
        "project": cfg.name, "codes": archive.codes(cfg), "generated": iso(), "cfg_dir": tables.where if tables else None,
        "ebs": len(recs), "verdicts": dict(verdicts),
        "by_config": {c: {"ebs": len(v), "total_gb": round(sum(v), 1), "median_gb": round(statistics.median(v), 1),
                          "max_gb": round(max(v), 1)} for c, v in sorted(by_config.items())},
        "targets_found": dict(sorted(targets.items())),
        "fields_missing_from_cfgs": {f: sorted(ebs) for f, ebs in sorted(missing.items())},
        "skip_candidates": {eb: res.summary() for eb, res in results.items() if res.verdict == "skip_candidate"},
        "failed_preflight": {eb: res.summary() for eb, res in results.items() if res.verdict == "preflight_failed"},
        "setup_variants": {"main": main, "signatures": dict(sigs.most_common()),
                           "outside_main": sorted(eb for eb, r in kept.items() if r["spw_signature"] != main),
                           "varying_within_eb": sorted(eb for eb, r in kept.items() if r["spw_varies"])},
        "archive_status": dict(Counter(r["archive_status"] for r in recs)),
        "excluded": excluded,
        "quota": {"total_sdm_tb": round(total / 1e3, 3), "disk_factor": cfg.pipeline.disk_factor,
                  "calibrate_workers": workers,
                  "peak_active_tb": round(sum(sizes[:workers]) * cfg.pipeline.disk_factor / 1e3, 3),
                  "finished_splits_tb": round(total * SPLIT_FACTOR / 1e3, 3),
                  "products_tb": round(total * PRODUCTS_FACTOR * ITERATIONS / 1e3, 3)},
        "clean": not verdicts.get("preflight_failed"),
    }


def text(rep: dict) -> str:
    out = [f"init report for {rep['project']} ({', '.join(rep['codes'])}) at {rep['generated']}",
           f"  pipeline cfgs: {rep['cfg_dir']}", "",
           f"{rep['ebs']} EBs in scope: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["verdicts"].items())),
           "by array configuration:"]
    out += [f"  {c:<3}{v['ebs']:>4} EBs  total {v['total_gb']:>9.1f} GB  median {v['median_gb']:>7.1f}  max {v['max_gb']:>7.1f}"
            for c, v in rep["by_config"].items()]
    out += ["", "targets found: " + (", ".join(f"{t} ({n} EBs)" for t, n in rep["targets_found"].items()) or "none")]
    if rep["fields_missing_from_cfgs"]:
        out.append("FIELDS MISSING FROM THE CFGS (add each to the target or calibrator cfg, then rerun):")
        out += [f"  {f}: {len(ebs)} EB(s), e.g. {ebs[0]}" for f, ebs in rep["fields_missing_from_cfgs"].items()]
    if rep["failed_preflight"]:
        out.append(f"preflight FAILED for {len(rep['failed_preflight'])} EB(s):")
        out += [f"  {eb}: {why}" for eb, why in rep["failed_preflight"].items()]
    if rep["skip_candidates"]:
        out.append(f"{len(rep['skip_candidates'])} skip candidate(s) (a human decides: include or skip):")
        out += [f"  {eb}: {why}" for eb, why in rep["skip_candidates"].items()]
    sv = rep["setup_variants"]
    out += ["", f"correlator setups: {len(sv['signatures'])} variant(s); {len(sv['outside_main'])} EB(s) outside the "
                f"main one, {len(sv['varying_within_eb'])} varying within an EB",
            "archive status: " + ", ".join(f"{k} {v}" for k, v in rep["archive_status"].items())]
    if rep["excluded"]:
        out.append(f"{len(rep['excluded'])} EB(s) excluded from the project:")
        out += [f"  {eb}: {why}" for eb, why in rep["excluded"].items()]
    q = rep["quota"]
    out += ["", f"SDM volume {q['total_sdm_tb']} TB. Quota estimate (first estimates; TAP size x factors):",
            f"  peak while calibrating: {q['peak_active_tb']} TB ({q['calibrate_workers']} largest EBs x disk_factor {q['disk_factor']})",
            f"  kept science splits at the end: {q['finished_splits_tb']} TB; QA products ({ITERATIONS} iterations): {q['products_tb']} TB",
            "", "CLEAN: no EB fails preflight" if rep["clean"] else "NOT CLEAN: fix the cfgs above and rerun"]
    return "\n".join(out)


def sheets_step(cfg, root: Root, make_client=None) -> tuple[list[str], int]:
    """Build/check both spreadsheets. Returns (report lines, exit code). make_client(id) -> client
    (tests pass FakeSheets; default gsheets.SheetsClient with the project's key)."""
    from . import gsheets, sheets_setup
    if not cfg.sheets.enabled:
        return ["sheets: sheets.enabled is false in project.yaml; nothing to build"], 2
    lease = read_json(root.lease)
    if lease and not lease.get("released") and (age_s(lease.get("heartbeat")) or 1e9) < 3 * cfg.controller.poll_s:
        return [f"sheets: controller {lease['holder']} holds the lease; stop it first "
                "(the controller is the only writer of sheet rows)"], 2
    try:
        sa = gsheets.service_account_email(cfg.sheets_key)
    except gsheets.KeyFileError as e:
        return [f"sheets: {e}"], 2
    out, code, reports = [f"sheets: service account {sa}"], 0, []
    for which, sid in (("tracker", cfg.sheets.tracker_id), ("flagbook", cfg.sheets.flagbook_id)):
        client = make_client(sid) if make_client else gsheets.SheetsClient(sid, credentials=cfg.sheets_key)
        try:
            rep = sheets_setup.build(cfg, client, which, sa)
        except gsheets.SheetsError as e:
            hint = (" (is the spreadsheet shared with the service account as Editor?)"
                    if e.status in (403, 404) else "")
            out.append(f"  {which} {sid}: FAILED: {e}{hint}")
            code = 2
            continue
        reports.append(rep)
        out.append(f"  {which} {sid}: created {rep['created'] or 'nothing'}; existing {rep['existing'] or 'none'}"
                   + (f"; deleted {rep['deleted']}" if rep["deleted"] else ""))
        out += [f"    PROBLEM: {p}" for p in rep["problems"]]
        code = code or (1 if rep["problems"] else 0)
    if reports:
        out.append("  protected-range editors: " + ", ".join(reports[0]["editors"]))
        out.append("  share by hand: reviewers as Editor on both spreadsheets (they can edit only the "
                   "sign-up cells and the flag rows/verdict cells); keep the service account as Editor")
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    (root.path / "reports").mkdir(parents=True, exist_ok=True)
    write_json(root.path / "reports" / f"sheets-{stamp}.json", {"generated": iso(), "service_account": sa,
                                                               "spreadsheets": reports})
    return out, code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True)
    p.add_argument("--root", help="override project.root")
    p.add_argument("--no-path-check", action="store_true", help="skip the CASA / cfg path checks of the config")
    p.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    p.add_argument("--sheets", action="store_true", help="also build/check the tracker and flag-book tabs")
    a = p.parse_args(argv)
    try:
        cfg = projconfig.load(a.project, root=a.root, check_paths=not a.no_path_check)
    except projconfig.ConfigError as e:
        sys.exit(str(e))
    if not archive.enabled(cfg) and not a.sheets:
        sys.exit("nothing to sync: set project.vla_project_codes and inputs.eb_list (archive or file:)")
    cfg.root.mkdir(parents=True, exist_ok=True)
    root = Root(cfg.root)
    sheet_code = 0
    if a.sheets:
        lines, sheet_code = sheets_step(cfg, root)
        print("\n".join(lines))
        if not archive.enabled(cfg):
            return sheet_code
    try:
        tables = cfgs.load(cfg)
    except cfgs.CfgError as e:
        sys.exit(f"pipeline cfgs: {e}")
    res = archive.sync(root, cfg)
    if not res.ok:
        sys.exit(f"archive sync failed: {res.error}")
    sig = preflight.main_signature(res.kept.values())
    included = lambda r: any(str(x) in (r["sdm"], str(r["ebid"])) for x in cfg.archive.include_ebs)
    results = {eb: preflight.run(r, cfg, tables, included=included(r), main_sig=sig) for eb, r in res.kept.items()}
    rep = build(cfg, res.kept, res.excluded, results, tables)
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    reports = root.path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    n = 1
    while (reports / f"init-{stamp}.json").exists() or (reports / f"init-{stamp}.txt").exists():
        n += 1                                  # two runs in one second must not overwrite each other
        stamp = f"{now().strftime('%Y%m%dT%H%M%SZ')}-{n}"
    write_json(reports / f"init-{stamp}.json", rep)
    (reports / f"init-{stamp}.txt").write_text(text(rep) + "\n")
    print(json.dumps(rep, indent=1) if a.json else text(rep))
    return sheet_code or (0 if rep["clean"] else 1)


if __name__ == "__main__":
    sys.exit(main())
