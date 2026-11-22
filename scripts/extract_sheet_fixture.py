"""Extract one project's rows from the old tracker workbook as a small test fixture.

    python3 scripts/extract_sheet_fixture.py <workbook.xlsx> 24B-224 [--out tests/fixtures]

Reads the 'Track Summary' style tabs ('20A - OpLog Summary', 'Archival Track Summary') and
writes only Trackname, Target, SBID, EBID, Configuration for the rows whose Trackname starts
with the project code: no reviewer, personnel or status columns. Needs openpyxl.
"""
import argparse
import csv
from pathlib import Path

import openpyxl

TABS = ("20A - OpLog Summary", "Archival Track Summary")
KEEP = ("Trackname", "Target", "SBID", "EBID", "Configuration")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("workbook")
    p.add_argument("code")
    p.add_argument("--out", default="tests/fixtures")
    a = p.parse_args()
    wb = openpyxl.load_workbook(a.workbook, read_only=True, data_only=True)
    rows = {}
    for tab in TABS:
        if tab not in wb.sheetnames:
            continue
        it = wb[tab].iter_rows(values_only=True)
        header = list(next(it))
        idx = [header.index(k) for k in KEEP]
        for r in it:
            r = tuple(r) + (None,) * len(header)
            name = r[idx[0]]
            if name and str(name).startswith(a.code):
                rows[name] = ["" if r[i] is None else (int(r[i]) if isinstance(r[i], float) else r[i]) for i in idx]
    out = Path(a.out) / f"sheet_{a.code}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(KEEP)
        w.writerows(sorted(rows.values()))
    print(f"{len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
