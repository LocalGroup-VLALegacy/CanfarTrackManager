"""One-off live check of the archive sync against the old tracker workbook (not part of pytest).

    python3 scripts/check_archive_vs_sheet.py <workbook.xlsx> 20A-346 [--tab "20A - OpLog Summary"]

Queries TAP for the project, aggregates it with trackmanager.archive, and compares with the
workbook's Trackname / Configuration / EBID columns: EB counts, image rows, EBs in both, and
disagreements. Needs network access and openpyxl.
"""
import argparse
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trackmanager import archive  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("workbook")
    p.add_argument("code")
    p.add_argument("--tab", default=None)
    p.add_argument("--tap-url", default="https://data-query.nrao.edu/tap")
    a = p.parse_args()
    rows = archive.TapClient(a.tap_url).query(a.code)
    images = [r for r in rows if r["access_format"] != "Execution Block"]
    agg = archive.aggregate(rows)
    recs = {k: v[0] for k, v in agg.items()}
    wb = openpyxl.load_workbook(a.workbook, read_only=True, data_only=True)
    tabs = [a.tab] if a.tab else ["20A - OpLog Summary", "Archival Track Summary"]
    sheet = {}
    for tab in tabs:
        if tab not in wb.sheetnames:
            continue
        it = wb[tab].iter_rows(values_only=True)
        h = list(next(it))
        for r in it:
            r = tuple(r) + (None,) * len(h)
            if r[h.index("Trackname")] and str(r[h.index("Trackname")]).startswith(a.code):
                sheet[r[h.index("Trackname")]] = (r[h.index("Configuration")], r[h.index("EBID")])
    both = sorted(set(sheet) & set(recs))
    cfg_bad = [e for e in both if sheet[e][0] != recs[e]["config"]]
    eb_bad = [e for e in both if sheet[e][1] is not None and int(sheet[e][1]) != recs[e]["ebid"]]
    print(f"TAP scan rows {len(rows)}, image rows {len(images)}; EBs in TAP {len(recs)}; EBs in sheet {len(sheet)}")
    print(f"in both {len(both)}; config disagreements {len(cfg_bad)}; EBID disagreements {len(eb_bad)}")
    for e in cfg_bad:
        print(f"  config differs: {e}: sheet {sheet[e][0]!r}, TAP {recs[e]['config']!r}")
    for e in eb_bad:
        print(f"  EBID differs: {e}: sheet {sheet[e][1]!r}, TAP {recs[e]['ebid']!r}")
    print(f"only in sheet {sorted(set(sheet) - set(recs))}")
    short = sorted(set(recs) - set(sheet))
    print(f"only in TAP {len(short)}:")
    for e in short:
        r = recs[e]
        print(f"  {e}  {r['config']}  {r['duration_min']:.0f} min  {r['est_size_gb']:.1f} GB")
    return 0 if not (images or cfg_bad or eb_bad) else 1


if __name__ == "__main__":
    sys.exit(main())
