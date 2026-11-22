"""Archive discovery: the NRAO TAP query, one record per Execution Block, and the sync (plan §3.7).

Stdlib only, so the controller image needs no pyvo or astropy.

    TapClient(url).query(code)   rows (dicts, one per scan) for one VLA project code
    aggregate(rows, ...)         {sdm: (record, scans)}, one record per obs_publisher_did
    select(records, cfg)         apply bands_ghz / exclude_ebs / eb_list / include_ebs
    sync(root, cfg, tap)         write tracks/<EB>/archive.json (+ archive_scans.csv) and report
                                 what is new, changed or missing; never removes an EB

`archive.tap_url` may be `file:///path/to/fixture.csv[.gz]` (columns as in
scripts/record_tap_fixture.py). The same filter as the live query is applied to it, so tests
and offline runs use exactly the code path of the real archive.

A failed or overflowing query changes nothing (all-or-nothing across project codes) and is
counted in archive/sync.json; `alarm()` is true after ALARM_AFTER consecutive failures.
"""
from __future__ import annotations

import csv
import dataclasses as dc
import gzip
import hashlib
import io
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from .store import age_s, iso, now, read_json, write_json

COLUMNS = ("obs_publisher_did", "obs_id", "project_code", "target_name", "scan_num", "s_ra", "s_dec",
           "t_min", "t_max", "t_exptime", "t_resolution", "freq_min", "freq_max", "configuration",
           "num_antennas", "access_estsize", "access_url", "access_format", "dataproduct_type",
           "spw_names", "center_frequencies", "bandwidths", "nums_channels", "spectral_resolutions",
           "proprietary_status", "qa_notes")
# The filter is required: without it 24B-224 returns ~1,565 image products next to its 47 EBs.
EB_FILTER = {"access_format": "Execution Block", "dataproduct_type": "visibility"}
MAXREC = 200_000
ALARM_AFTER = 3          # consecutive failed syncs before the dashboard alarm
RETRY_AFTER_S = 3600     # after a failed sync, try again after this long (or poll_h if shorter)
DID = re.compile(r"^(?P<code>[^.]+)\.sb(?P<sbid>\d+)\.eb(?P<ebid>\d+)\.(?P<mjd>\d+\.\d+)$")
# Older EBs of some projects are named 20A-346_sb<sbid>_<n>_<m>.<mjd>: no EB id, same SDM-name use.
DID_OLD = re.compile(r"^(?P<code>[^._]+)_sb(?P<sbid>\d+)_\d+_\d+\.(?P<mjd>\d+\.\d+)$")


class TapError(RuntimeError):
    """The archive could not give a complete answer; nothing is changed."""


# ---- TAP client ---------------------------------------------------------------------------
def adql(code: str) -> str:
    return (f"SELECT {', '.join(COLUMNS)} FROM tap_schema.obscore WHERE project_code = '{code}' "
            + " ".join(f"AND {k} = '{v}'" for k, v in EB_FILTER.items()))


def parse_votable(data: bytes):
    """Minimal VOTable TABLEDATA reader: (column names, rows, query status dict or None)."""
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


def _eb_rows(rows):
    return [r for r in rows if all(r.get(k) == v for k, v in EB_FILTER.items())]


class TapClient:
    def __init__(self, url: str, timeout: float = 180, maxrec: int = MAXREC):
        self.url, self.timeout, self.maxrec = url, timeout, maxrec

    def query(self, code: str) -> list[dict]:
        if self.url.startswith("file://"):
            return self._from_file(Path(urllib.parse.unquote(self.url[len("file://"):])), code)
        body = urllib.parse.urlencode({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "votable",
                                       "QUERY": adql(code), "MAXREC": self.maxrec}).encode()
        try:
            req = urllib.request.Request(self.url.rstrip("/") + "/sync", body)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except (urllib.error.URLError, OSError) as e:  # includes HTTPError and timeouts
            raise TapError(f"TAP request failed: {e!r}") from e
        try:
            names, rows, status = parse_votable(data)
        except ET.ParseError as e:
            raise TapError(f"TAP answer is not a VOTable: {e}") from e
        if status and status["value"] not in ("OK", None):
            raise TapError(f"TAP query status {status['value']}: {status['text']}"
                           + (" (async TAP is not implemented)" if status["value"] == "OVERFLOW" else ""))
        if len(rows) >= self.maxrec:
            raise TapError(f"TAP returned {len(rows)} rows = MAXREC; the list may be short "
                           "(async TAP is not implemented)")
        return [dict(zip(names, r)) for r in rows]

    @staticmethod
    def _from_file(path: Path, code: str) -> list[dict]:
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", newline="") as f:
                rows = list(csv.DictReader(f))
        except (OSError, EOFError, csv.Error) as e:
            raise TapError(f"cannot read TAP fixture {path}: {e!r}") from e
        return _eb_rows([r for r in rows if r.get("project_code") == code])


# ---- aggregation --------------------------------------------------------------------------
def parse_did(did: str) -> dict:
    m = DID.match(did)
    if m:
        return {"sdm": did, "ebid": int(m["ebid"]), "sbid": int(m["sbid"]), "mjd": float(m["mjd"])}
    m = DID_OLD.match(did)
    if m:
        return {"sdm": did, "ebid": None, "sbid": int(m["sbid"]), "mjd": float(m["mjd"])}
    return {"sdm": did, "ebid": None, "sbid": None, "mjd": None}


def _f(row, key, default=None):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return default


def _spw_signature(row) -> str:
    key = "|".join(row.get(k, "") for k in ("spw_names", "center_frequencies", "bandwidths", "nums_channels"))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


SCAN_COLUMNS = ("scan_num", "target_name", "t_min", "t_max", "t_exptime", "s_ra", "s_dec", "spw_signature")


def aggregate(rows: list[dict], size_calibration: float = 1.0) -> dict:
    """One record per obs_publisher_did (already the SDM name the pipeline needs).

    Sizes: `access_estsize` is in kB and repeated on every scan of an EB, so it is taken once,
    not summed. Times are MJD days. Frequencies are Hz in TAP and GHz in the record."""
    by_eb = defaultdict(list)
    for r in _eb_rows(rows):
        by_eb[r["obs_publisher_did"]].append(r)
    out = {}
    for did, scans in sorted(by_eb.items()):
        scans.sort(key=lambda r: (_f(r, "t_min", 0.0), _f(r, "scan_num", 0.0)))
        first = scans[0]
        on_source = defaultdict(float)
        for r in scans:
            on_source[r["target_name"]] += _f(r, "t_exptime", 0.0)
        start = min(_f(r, "t_min", float("inf")) for r in scans)
        end = max(_f(r, "t_max", float("-inf")) for r in scans)
        sigs = [_spw_signature(r) for r in scans]
        fmin = [_f(r, "freq_min") for r in scans if _f(r, "freq_min") is not None]
        fmax = [_f(r, "freq_max") for r in scans if _f(r, "freq_max") is not None]
        est_kb = _f(first, "access_estsize", 0.0) or 0.0
        rec = {**parse_did(did),
               "obs_id": first.get("obs_id", ""), "project_code": first.get("project_code", ""),
               "start": start, "end": end, "duration_min": round((end - start) * 1440, 3),
               "on_source_s": round(sum(on_source.values()), 2),
               "on_source_by_field": {k: round(v, 2) for k, v in on_source.items()},
               "config": first.get("configuration", ""),
               "n_ant": int(_f(first, "num_antennas", 0) or 0), "int_s": _f(first, "t_resolution"),
               "fields": list(dict.fromkeys(r["target_name"] for r in scans)),
               "n_scans": len(scans),
               "freq_min_ghz": round(min(fmin) / 1e9, 6) if fmin else None,
               "freq_max_ghz": round(max(fmax) / 1e9, 6) if fmax else None,
               "spw_signature": Counter(sigs).most_common(1)[0][0], "spw_varies": len(set(sigs)) > 1,
               "est_size_gb": round(est_kb / 1e6 * size_calibration, 4),
               "archive_status": first.get("proprietary_status", ""), "archive_url": first.get("access_url", ""),
               "qa_notes": next((r["qa_notes"] for r in scans if r.get("qa_notes")), "")}
        table = [{**{k: r.get(k, "") for k in SCAN_COLUMNS if k != "spw_signature"}, "spw_signature": s}
                 for r, s in zip(scans, sigs)]
        out[did] = (rec, table)
    return out


# ---- selection ----------------------------------------------------------------------------
def read_eb_list(path) -> set:
    """Allow-list from a CSV: the `eb` column if there is one, else the first column."""
    with open(path, newline="") as f:
        rows = [r for r in csv.reader(f) if r and r[0].strip()]
    if not rows:
        return set()
    header = [c.strip().lower() for c in rows[0]]
    col = header.index("eb") if "eb" in header else 0
    body = rows[1:] if "eb" in header else rows
    return {r[col].strip() for r in body if len(r) > col and r[col].strip()}


def _listed(rec, names) -> bool:
    return rec["sdm"] in names or (rec["ebid"] is not None and str(rec["ebid"]) in {str(n) for n in names})


def in_band(rec, bands) -> bool:
    lo, hi = rec.get("freq_min_ghz"), rec.get("freq_max_ghz")
    if lo is None or hi is None:
        return True  # no frequency in TAP: let preflight and a human decide
    return any(lo <= b[1] and hi >= b[0] for b in bands)


def select(records: dict, cfg, allow: set | None = None):
    """(kept {sdm: record}, excluded {sdm: reason}). `include_ebs` beats every filter."""
    a = cfg.archive
    kept, excluded = {}, {}
    for sdm, rec in records.items():
        if _listed(rec, a.include_ebs):
            kept[sdm] = rec
        elif _listed(rec, a.exclude_ebs):
            excluded[sdm] = "listed in archive.exclude_ebs"
        elif allow is not None and not _listed(rec, allow):
            excluded[sdm] = "not in inputs.eb_list"
        elif not in_band(rec, a.bands_ghz):
            excluded[sdm] = (f"outside archive.bands_ghz ({rec['freq_min_ghz']}-{rec['freq_max_ghz']} GHz)")
        else:
            kept[sdm] = rec
    return kept, excluded


# ---- sync ---------------------------------------------------------------------------------
@dc.dataclass
class SyncResult:
    ok: bool = True
    error: str | None = None
    kept: dict = dc.field(default_factory=dict)        # sdm -> record
    new: list = dc.field(default_factory=list)
    changed: dict = dc.field(default_factory=dict)     # sdm -> {field: [old, new]}
    missing: list = dc.field(default_factory=list)     # newly missing since the last sync
    excluded: dict = dc.field(default_factory=dict)    # sdm -> reason
    rows: int = 0


def _write_scans(path: Path, table):
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SCAN_COLUMNS)
    w.writeheader()
    w.writerows(table)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(buf.getvalue())
    tmp.replace(path)


def client(cfg) -> TapClient:
    return TapClient(cfg.archive.tap_url)


def codes(cfg) -> list:
    return list(cfg.project.vla_project_codes or [])


def enabled(cfg) -> bool:
    return bool(codes(cfg)) and (cfg.inputs.eb_list == "archive" or cfg.inputs.eb_list.startswith("file:"))


def sync(root, cfg, tap: TapClient | None = None, when=None) -> SyncResult:
    """One archive sync. Never removes anything: an EB that disappears from the result is only
    reported as missing (once)."""
    tap = tap or client(cfg)
    when = when or now()
    state_file = root.archive_dir / "sync.json"
    prev = read_json(state_file, {})
    try:
        rows = []
        for code in codes(cfg):
            rows += tap.query(code)
        allow = read_eb_list(cfg.resolve(cfg.inputs.eb_list[len("file:"):])) \
            if cfg.inputs.eb_list.startswith("file:") else None
    except (TapError, OSError) as e:
        write_json(state_file, {**prev, "last_attempt": iso(when), "error": str(e),
                                "consecutive_failures": prev.get("consecutive_failures", 0) + 1})
        return SyncResult(ok=False, error=str(e))

    agg = aggregate(rows, cfg.archive.size_calibration)
    kept, excluded = select({k: v[0] for k, v in agg.items()}, cfg, allow)
    index = read_json(root.archive_dir / "index.json", {"kept": [], "missing": []})
    res = SyncResult(kept=kept, excluded=excluded, rows=len(rows))
    for sdm, rec in kept.items():
        old = read_json(root.archive_json(sdm))
        if old is None:
            res.new.append(sdm)
        elif old != rec:
            res.changed[sdm] = {k: [old.get(k), rec.get(k)] for k in sorted(set(old) | set(rec))
                                if old.get(k) != rec.get(k)}
        if old != rec:
            write_json(root.archive_json(sdm), rec)
            _write_scans(root.archive_scans(sdm), agg[sdm][1])
    gone = sorted(set(index["kept"]) - set(agg) - set(excluded))
    res.missing = [e for e in gone if e not in index.get("missing", [])]
    if res.new or res.changed or res.missing:  # raw rows kept only when something changed
        stem = when.strftime("%Y%m%dT%H%M%SZ")
        snap = root.archive_dir / "snapshots" / f"{stem}.csv.gz"
        snap.parent.mkdir(parents=True, exist_ok=True)
        for n in range(2, 100):  # two syncs within one second must not overwrite each other
            if not snap.exists():
                break
            snap = snap.with_name(f"{stem}-{n}.csv.gz")
        with gzip.open(snap, "wt", newline="") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
            w.writeheader()
            w.writerows(rows)
    write_json(root.archive_dir / "index.json",
               {"kept": sorted(set(kept) | (set(index["kept"]) & set(gone))), "missing": sorted(gone),
                "excluded": excluded, "updated": iso(when)})
    write_json(state_file, {"last_ok": iso(when), "last_attempt": iso(when), "error": None,
                            "consecutive_failures": 0, "rows": len(rows), "ebs": len(kept)})
    return res


def due(root, cfg, when=None) -> bool:
    """True on the first cycle, then every archive.poll_h after the last good sync (sooner after a failure)."""
    st = read_json(root.archive_dir / "sync.json")
    if not st:
        return True
    if st.get("consecutive_failures"):
        return (age_s(st.get("last_attempt")) or 0) >= min(cfg.archive.poll_h * 3600, RETRY_AFTER_S)
    return (age_s(st.get("last_ok")) or 0) >= cfg.archive.poll_h * 3600


def alarm(root) -> dict | None:
    st = read_json(root.archive_dir / "sync.json") or {}
    if st.get("consecutive_failures", 0) >= ALARM_AFTER:
        return {"consecutive_failures": st["consecutive_failures"], "last_ok": st.get("last_ok"),
                "error": st.get("error")}
    return None
