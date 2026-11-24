"""Text dashboard for a run: heartbeat, lease, per-EB stages and the job database.

    python -m trackmanager.status --root ../runs/r1 [--events 15]
"""
import argparse
from pathlib import Path

from .store import Root, age_s, read_json, read_jsonl


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--events", type=int, default=12, help="number of recent events to show")
    a = p.parse_args(argv)
    r = Root(Path(a.root).resolve())

    hb, lease = read_json(r.heartbeat, {}), read_json(r.lease, {})
    hb_age = age_s(hb.get("ts"))
    print(f"root       {r.path}")
    print(f"controller {hb.get('holder')} on {hb.get('host')}, cycle {hb.get('cycle')}, "
          f"heartbeat {'never' if hb_age is None else f'{hb_age:.0f}s ago'}")
    print(f"lease      holder={lease.get('holder')} released={lease.get('released')} "
          f"handoff_to={lease.get('handoff_to')} predecessor={lease.get('predecessor')}")
    print(f"cert       not after {hb.get('cert_not_after')} (file mtime {hb.get('cert_mtime')})")

    full = read_json(r.summary, {})
    arc = full.get("archive")
    if arc:
        print(f"archive    last ok {arc.get('last_ok')}, {arc.get('ebs')} EBs, {arc.get('consecutive_failures', 0)} failed syncs in a row"
              + (f"  ALARM: {arc['alarm']['error']}" if arc.get("alarm") else ""))
    sh = full.get("sheets")
    if sh:
        h = sh.get("health") or {}
        print(f"sheets     last ok {h.get('last_ok')}, {h.get('consecutive_failures', 0)} failed steps in a row, "
              f"{sh.get('flag_tabs', 0)} flag tabs, verdicts waiting {sh.get('pending_verdicts') or 'none'}"
              + (f"  ALARM: {sh['alarm']}" if sh.get("alarm") else ""))
        for item in sh.get("needs_you") or []:
            print(f"  needs you: {item['eb']}: {item['item']}: {item['detail']}")
    w = full.get("workers") or {}
    if w:
        by = w.get("in_use_by_kind") or {}
        slots = ", ".join(f"{k} {by.get(k, 0)}/{(w.get('limits') or {}).get(k, 0)}" for k in by)
        print(f"workers    {slots or str(w.get('in_use')) + '/' + str(w.get('limit'))} slots in use ({w.get('running_tracks')} tracks, "
              f"{len(w.get('orphans', []))} orphan sessions {w.get('orphans') or ''}); "
              f"{w.get('queued')} queued, {w.get('held')} held")
    d = full.get("disk") or {}
    if d.get("quota_tb") is not None:
        print(f"disk       {d['modelled_used_tb']} of {d['quota_tb']} TB reserved by the model "
              f"(keeping {d['min_free_tb']} TB free; {d['free_tb']} TB left; disk_factor {d['disk_factor']}"
              f"{'; ' + str(d['unsized_tracks']) + ' tracks of unknown size' if d.get('unsized_tracks') else ''})")
    elif d:
        print(f"disk       gate off (controller.quota_tb is not set); model holds {d['modelled_used_tb']} TB")
    summ = full.get("ebs", {})
    wid = max([14] + [len(eb) + 2 for eb in summ])
    print(f"\n{'EB':<{wid}}{'stage':<24}{'iter':>4}{'att':>4}  {'hold':<5}{'queue':>6}  detail")
    for eb, st in sorted(summ.items(), key=lambda kv: (kv[1].get("queue_position") or 0, kv[0])):
        stage = st["stage"] + ("/" + st["activity"] if st.get("activity") else "")
        blocked = f" [blocked by {st['blocked_by']}]" if st.get("blocked_by") else ""
        print(f"{eb:<{wid}}{stage:<24}{st.get('iteration') or 1:>4}{st['attempt']:>4}  "
              f"{str(bool(st.get('hold'))):<5}{str(st.get('queue_position') or ''):>6}  {st.get('detail')}{blocked}")

    jobs = read_json(r.jobs_summary, {})
    jw = max([18] + [len(k) + 2 for k in jobs])
    print(f"\n{'job (eb#iter,att)':<{jw}}{'session':<10}{'last status':<20}{'platform':<11}{'class':<10}"
          f"{'exit':>5}{'wall_s':>8}{'rss_MB':>8}{'disk_GB':>8}")
    for key, j in sorted(jobs.items()):
        print(f"{key:<{jw}}{str(j.get('job_id')):<10}{str(j.get('status')):<20}"
              f"{str(j.get('platform_status', '')):<11}{str(j.get('failure_class', '')):<10}"
              f"{str(j.get('exit_code', '')):>5}{str(j.get('wall_s', '')):>8}"
              f"{str(j.get('peak_rss_mb', '')):>8}{str(j.get('peak_disk_gb', '')):>8}")

    ev = read_jsonl(r.events)[-a.events:]
    print(f"\nlast {len(ev)} events")
    for e in ev:
        print(f"  {e['ts']} {e['eb']:<12} {e['from']} -> {e['to']}: {e['reason'][:110]}")


if __name__ == "__main__":
    main()
