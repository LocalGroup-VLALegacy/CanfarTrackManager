"""Rebuild and check manual flag commands from a flag tab's raw columns (plan §3.5, Ingest).

The flag tab's "Flag string" column is only the reviewer's preview. The controller builds each
command itself from the raw cells, so a formula slip or an apostrophe in a description can't
produce a broken command, and it checks every applied row against the track's archive metadata
before a multi-hour run is started. Output format matches the template formula and the
existing manual_flagging.txt files (two header lines, then one command per line).

    cmds, problems = build_commands(rows, archive)    # rows: dicts keyed by header name
    text = flag_file(archive["sdm"], archive["config"], cmds)
A verdict is applied only when `problems` is empty; otherwise each problem goes to the row's
"Check" cell and a summary to M2.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import re

FIELDS = ("Field Name", "Scan", "SPW", "Antenna Name", "Start Date/Time", "End Date/Time",
          "UVrange", "Correlation")
SCAN_RE = re.compile(r"^\d+(~\d+)?(,\d+(~\d+)?)*$")
SPW_RE = re.compile(r"^\d+(~\d+)?(:\d+~\d+(;\d+~\d+)*)?(,\d+(~\d+)?(:\d+~\d+(;\d+~\d+)*)?)*$")
ANT_TOKEN = re.compile(r"^ea\d\d(&{1,3}(ea\d\d)?)?$")
TIME_FMT = "%Y/%m/%d/%H:%M:%S"
UV_RE = re.compile(r"^(<|>)?\d+(\.\d+)?(~\d+(\.\d+)?)?(m|km|lambda|klambda|Mlambda)?$")
CORRS = {"", "*", "LL", "RR", "LR", "RL"}


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _truthy(v) -> bool:
    return v is True or _s(v).upper() == "TRUE"


def _clean_reason(text: str) -> str:
    """Quotes would end the reason='...' string early; drop them, collapse whitespace."""
    return " ".join(text.replace("'", "").replace('"', "").split())


def _ids(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        part = part.split(":")[0]
        a, _, b = part.partition("~")
        out += list(range(int(a), int(b or a) + 1))
    return out


def check_row(row: dict, archive: dict) -> list[str]:
    """Problems with one applied row (empty list = fine)."""
    p = []
    v = {k: _s(row.get(k)) for k in FIELDS}
    if not any(v.values()):
        return ["row is ticked but has no selection: it would flag the whole track"]
    fields = archive.get("fields") or []
    f = v["Field Name"]
    if f and f != "*" and fields:
        names = [x.strip() for x in f.split(",")]
        for n in names:
            if any(ch in n for ch in "*?[") :
                if not fnmatch.filter(fields, n):
                    p.append(f"field pattern {n!r} matches no field of this track")
            elif n not in fields:
                p.append(f"field {n!r} is not in this track ({', '.join(fields)})")
    if v["Scan"]:
        if not SCAN_RE.match(v["Scan"]):
            p.append(f"scan {v['Scan']!r}: use 5, 5,6 or 5~10")
        elif archive.get("scans") and (bad := sorted(set(_ids(v["Scan"])) - set(archive["scans"]))):
            p.append(f"scan(s) {bad} not in this track (1-{max(archive['scans'])})")
    if v["SPW"]:
        if not SPW_RE.match(v["SPW"]):
            p.append(f"spw {v['SPW']!r}: use 3, 0,1,3, 1~4 or 3:0~14")
        elif archive.get("n_spw") and max(_ids(v["SPW"])) >= archive["n_spw"]:
            p.append(f"spw {v['SPW']!r}: this track has spws 0-{archive['n_spw'] - 1}")
    if v["Antenna Name"]:
        toks = v["Antenna Name"].split(",")
        if not all(ANT_TOKEN.match(t) for t in toks):
            p.append(f"antenna {v['Antenna Name']!r}: use ea13 or ea13&ea18")
        elif archive.get("antennas"):
            named = set(re.findall(r"ea\d\d", v["Antenna Name"]))
            if missing := sorted(named - set(archive["antennas"])):
                p.append(f"antenna(s) {missing} not in this track")
    times = []
    for k in ("Start Date/Time", "End Date/Time"):
        if v[k]:
            try:
                times.append(dt.datetime.strptime(v[k], TIME_FMT))
            except ValueError:
                p.append(f"{k} {v[k]!r}: use YYYY/MM/DD/HH:mm:ss (no decimals)")
    if len(times) == 2 and times[0] > times[1]:
        p.append("start time is after end time")
    if times and archive.get("start") and archive.get("end"):
        t0 = dt.datetime.fromisoformat(archive["start"]) - dt.timedelta(hours=1)
        t1 = dt.datetime.fromisoformat(archive["end"]) + dt.timedelta(hours=1)
        if any(not (t0 <= t <= t1) for t in times):
            p.append(f"time outside the track ({archive['start']} to {archive['end']} UTC)")
    if v["UVrange"] and not UV_RE.match(v["UVrange"].replace(" ", "")):
        p.append(f"uvrange {v['UVrange']!r}: use e.g. 0~20klambda or <500m")
    if v["Correlation"] not in CORRS:
        p.append(f"correlation {v['Correlation']!r}: use LL, RR or leave blank")
    return p


def command(row: dict) -> str:
    """The flagdata command for one applied row, in the template's field order."""
    v = {k: _s(row.get(k)) for k in FIELDS}
    start, end = v["Start Date/Time"], v["End Date/Time"]
    timerange = f"{start}~{end}" if start and end else (f">{start}" if start else (f"<{end}" if end else ""))
    parts = ["mode='manual'"]
    if v["Field Name"] not in ("", "*"):
        parts.append(f"field='{v['Field Name']}'")
    for key, val in (("scan", v["Scan"]), ("spw", v["SPW"]), ("antenna", v["Antenna Name"]),
                     ("timerange", timerange), ("uvrange", v["UVrange"].replace(" ", ""))):
        if val:
            parts.append(f"{key}='{val}'")
    if v["Correlation"] not in ("", "*"):
        parts.append(f"correlation='{v['Correlation']}'")
    reason = f"[MANUAL] Issue: {_clean_reason(_s(row.get('Issue Description')))}. " \
             f"Flagged by {_clean_reason(_s(row.get('Name of QA person')))}"
    parts.append(f"reason='{reason}'")
    return " ".join(parts)


def build_commands(rows: list[dict], archive: dict, first_row: int = 7):
    """(commands, problems) for the ticked rows. problems: list of (sheet row, message)."""
    cmds, problems = [], []
    for i, row in enumerate(rows):
        if not _truthy(row.get("Apply Row")):
            continue
        errs = check_row(row, archive)
        if errs:
            problems += [(first_row + i, e) for e in errs]
        else:
            cmds.append(command(row))
    return cmds, problems


def flag_file(sdm: str, config: str, cmds: list[str]) -> str:
    """manual_flagging.txt contents (same header as gsheet_flagging.download_flagsheet_to_flagtxt)."""
    return f"# Manual flagging for track {sdm}\n# Config: {config}\n" + "".join(c + "\n" for c in cmds)
