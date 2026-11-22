"""Record a TAP result as a compact test fixture (stdlib only).

    python3 scripts/record_tap_fixture.py 24B-224 [--out tests/fixtures] [--filtered]

Queries tap_schema.obscore for one VLA project code with the columns the archive sync uses
(plan §3.7) and writes <out>/tap_<code>.csv.gz. By default the query is NOT restricted to
Execution Blocks, so image products stay in the fixture and a test can prove the sync's
filter removes them; `--filtered` applies the live filter instead (for large projects).
`trackmanager.archive.TapClient` reads this file when archive.tap_url is file://<path>.
"""
import argparse
import csv
import gzip
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOST = "https://data-query.nrao.edu/tap"
COLUMNS = ("obs_publisher_did", "obs_id", "project_code", "target_name", "scan_num", "s_ra", "s_dec",
           "t_min", "t_max", "t_exptime", "t_resolution", "freq_min", "freq_max", "configuration",
           "num_antennas", "access_estsize", "access_url", "access_format", "dataproduct_type",
           "spw_names", "center_frequencies", "bandwidths", "nums_channels", "spectral_resolutions",
           "proprietary_status", "qa_notes")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("code")
    p.add_argument("--out", default="tests/fixtures")
    p.add_argument("--filtered", action="store_true")
    p.add_argument("--url", default=HOST)
    p.add_argument("--maxrec", type=int, default=200000)
    a = p.parse_args()
    where = f"project_code = '{a.code}'"
    if a.filtered:
        where += " AND access_format = 'Execution Block' AND dataproduct_type = 'visibility'"
    adql = f"SELECT {', '.join(COLUMNS)} FROM tap_schema.obscore WHERE {where}"
    body = urllib.parse.urlencode({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "votable",
                                   "QUERY": adql, "MAXREC": a.maxrec}).encode()
    with urllib.request.urlopen(urllib.request.Request(a.url.rstrip("/") + "/sync", body), timeout=300) as r:
        root = ET.fromstring(r.read())
    strip = lambda t: t.split("}")[-1]
    for el in root.iter():
        if strip(el.tag) == "INFO" and el.get("name") == "QUERY_STATUS" and el.get("value") != "OK":
            sys.exit(f"TAP query status {el.get('value')}: {(el.text or '').strip()}")
    names = [el.get("name") for el in root.iter() if strip(el.tag) == "FIELD"]
    rows = [[(td.text or "") for td in tr if strip(td.tag) == "TD"] for tr in root.iter() if strip(tr.tag) == "TR"]
    out = Path(a.out) / f"tap_{a.code}.csv.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", newline="", compresslevel=9) as f:
        w = csv.writer(f)
        w.writerow(names)
        w.writerows(rows)
    print(f"{len(rows)} rows, {len(names)} columns -> {out} ({out.stat().st_size / 1e3:.0f} kB)")


if __name__ == "__main__":
    main()
