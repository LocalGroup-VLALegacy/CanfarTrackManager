"""Isolated NRAO TAP test: can this session query the archive for a known EB?

Standalone and stdlib-only (no pyvo/astropy), so it runs in skaha/base-notebook.
It queries tap_schema.obscore for the SDM found in the test-track folder, then
compares the archive metadata with the SDM on disk.

    python3 tap_probe.py [--track-dir DIR] [--tar TAR] [--out DIR] [--timeout S]
Headless:
    canfar create headless images.canfar.net/skaha/base-notebook:latest --name tm-tap \
        -- python3 <repo>/scripts/tap_probe.py --out <project space>/probes
Writes <out>/tap_<session>.json and prints a summary.
"""
import argparse
import datetime as dt
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOST = "data-query.nrao.edu"
SYNC = f"https://{HOST}/tap/sync"
COLUMNS = ("obs_publisher_did", "project_code", "target_name", "scan_num", "configuration",
           "access_estsize", "t_min", "t_max", "t_exptime", "num_antennas", "proprietary_status")


def iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def network_checks(timeout):
    out = {}
    t0 = time.time()
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)})
        out["dns"] = {"ok": True, "addresses": addrs, "s": round(time.time() - t0, 2)}
    except OSError as e:
        out["dns"] = {"ok": False, "error": repr(e), "s": round(time.time() - t0, 2)}
        return out
    t0 = time.time()
    try:
        with socket.create_connection((HOST, 443), timeout=timeout) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=HOST) as tls:
                out["tcp_tls"] = {"ok": True, "peer": tls.getpeername()[0], "tls": tls.version(),
                                  "s": round(time.time() - t0, 2)}
    except OSError as e:
        out["tcp_tls"] = {"ok": False, "error": repr(e), "s": round(time.time() - t0, 2)}
    return out


def parse_votable(data: bytes):
    """Minimal VOTable TABLEDATA reader: returns (column names, rows, query status)."""
    root = ET.fromstring(data)
    strip = lambda t: t.split("}")[-1]
    status = None
    for el in root.iter():
        if strip(el.tag) == "INFO" and el.get("name") == "QUERY_STATUS":
            status = {"value": el.get("value"), "text": (el.text or "").strip()}
    names = [el.get("name") for el in root.iter() if strip(el.tag) == "FIELD"]
    rows = [[(td.text or "") for td in tr if strip(td.tag) == "TD"]
            for tr in root.iter() if strip(tr.tag) == "TR"]
    return names, rows, status


def query(eb, timeout):
    adql = (f"SELECT {', '.join(COLUMNS)} FROM tap_schema.obscore "
            f"WHERE obs_publisher_did = '{eb}' AND access_format = 'Execution Block'")
    body = urllib.parse.urlencode({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "votable",
                                   "QUERY": adql, "MAXREC": 10000}).encode()
    t0 = time.time()
    res = {"adql": adql}
    try:
        with urllib.request.urlopen(urllib.request.Request(SYNC, body), timeout=timeout) as r:
            data = r.read()
            res.update(http=r.status, bytes=len(data))
    except urllib.error.HTTPError as e:
        res.update(ok=False, http=e.code, error=repr(e), s=round(time.time() - t0, 2))
        return res, None
    except (urllib.error.URLError, OSError) as e:
        res.update(ok=False, error=repr(e), s=round(time.time() - t0, 2))
        return res, None
    res["s"] = round(time.time() - t0, 2)
    names, rows, status = parse_votable(data)
    res.update(ok=bool(rows), query_status=status, nrows=len(rows))
    return res, [dict(zip(names, r)) for r in rows]


def summarise_archive(rows):
    num = lambda r, k: float(r[k]) if r.get(k) not in (None, "") else None
    targets = sorted({r["target_name"] for r in rows})
    return {"scans": len(rows), "fields": targets, "configuration": sorted({r["configuration"] for r in rows}),
            "est_size_bytes": int(num(rows[0], "access_estsize") * 1000),
            "duration_h": round((max(num(r, "t_max") for r in rows) - min(num(r, "t_min") for r in rows)) * 24, 3),
            "on_source_h": round(sum(num(r, "t_exptime") or 0 for r in rows) / 3600, 3),
            "num_antennas": sorted({r["num_antennas"] for r in rows}),
            "proprietary_status": sorted({r["proprietary_status"] for r in rows})}


def summarise_sdm(sdm: Path, tar: Path | None):
    read = lambda n: (sdm / n).read_text(errors="ignore")
    out = {"fields": sorted(set(re.findall(r"<fieldName>\s*(.*?)\s*</fieldName>", read("Field.xml")))),
           "scans": len(re.findall(r"<row>", read("Scan.xml")))}
    if tar and tar.exists():
        out["tar_bytes"] = tar.stat().st_size
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--track-dir", default="/arc/projects/VVINGS/ic10_d_testcase/test_track")
    p.add_argument("--tar", default="/arc/projects/VVINGS/ic10_d_testcase/ic10_d_sdm.tar")
    p.add_argument("--out", default="probes", help="output folder (default ./probes)")
    p.add_argument("--timeout", type=float, default=120)
    a = p.parse_args()

    track = Path(a.track_dir)
    sdms = [d for d in track.iterdir() if d.is_dir() and re.fullmatch(r"\d\d[AB]-\d+\.sb\d+\.eb\d+\.\d+\.\d+", d.name)]
    if len(sdms) != 1:
        sys.exit(f"expected one SDM directory in {track}, found {[d.name for d in sdms]}")
    sdm = sdms[0]
    session = os.environ.get("skaha_sessionid", "local")
    report = {"ts": iso(), "session": session, "host": socket.gethostname(),
              "python": sys.version.split()[0], "eb": sdm.name}

    report["network"] = network_checks(min(a.timeout, 30))
    report["query"], rows = query(sdm.name, a.timeout)
    report["sdm"] = summarise_sdm(sdm, Path(a.tar))
    if rows:
        arc = summarise_archive(rows)
        report["archive"] = arc
        report["checks"] = {
            "fields_match": arc["fields"] == report["sdm"]["fields"],
            "scan_count_match": arc["scans"] == report["sdm"]["scans"],
            "size_ratio_tar_over_est": (round(report["sdm"]["tar_bytes"] / arc["est_size_bytes"], 5)
                                        if "tar_bytes" in report["sdm"] else None),
        }

    out = Path(a.out) / f"tap_{session}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    q = report["query"]
    print(f"{report['ts']} session={session} eb={sdm.name}")
    print(f"  dns: {report['network'].get('dns')}")
    print(f"  tcp+tls: {report['network'].get('tcp_tls')}")
    print(f"  query: ok={q.get('ok')} http={q.get('http')} rows={q.get('nrows')} in {q.get('s')} s"
          f"{' error=' + q['error'] if q.get('error') else ''}")
    if rows:
        print(f"  archive: {report['archive']}")
        print(f"  sdm on disk: {report['sdm']}")
        print(f"  checks: {report['checks']}")
    print(f"  wrote {out}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
