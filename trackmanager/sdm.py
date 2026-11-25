"""Read what stage_local and the controller need from an SDM directory on disk (stdlib only).

    main_rows(sdm)      rows in Main.xml (one per BDF)
    bdf_count(sdm)      files in ASDMBinary/
    byte_size(sdm)      total bytes, symlinks inside the SDM not followed
    field_intents(sdm)  {field name: {"scans": n, "intents": [...]}} from Main.xml + Scan.xml + Field.xml
    antennas(sdm)       antenna names (ea01, ...) from Antenna.xml, for checking flag rows

Scan.xml stores intents as an ASDM array, "<rank> <count> <v1> ... <vN>" (e.g. "1 3 CALIBRATE_FLUX
CALIBRATE_BANDPASS CALIBRATE_DELAY"); older files carry a "#SUBINTENT" suffix, which is dropped.
The scan -> field join goes through Main.xml (`scanNumber`, `fieldId`), because a field's name is
not its source's name for mosaics.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path


class SdmError(ValueError):
    pass


def _rows(path: Path):
    """Yield each <row> of an SDM table as {tag: text}; memory stays flat for big Main.xml files."""
    try:
        for _, el in ET.iterparse(path, events=("end",)):
            if el.tag == "row":
                yield {c.tag: (c.text or "").strip() for c in el}
                el.clear()
    except (ET.ParseError, OSError) as e:
        raise SdmError(f"{path.name}: {e}") from e


def main_rows(sdm) -> int:
    return sum(1 for _ in _rows(Path(sdm) / "Main.xml"))


def bdf_count(sdm) -> int:
    d = Path(sdm) / "ASDMBinary"
    return sum(1 for p in d.iterdir()) if d.is_dir() else 0


def byte_size(sdm) -> int:
    """Bytes in the SDM tree (`du -sb`-like: files only). Symlinks inside it are counted as links."""
    total = 0
    stack = [os.path.realpath(sdm)]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    total += e.stat(follow_symlinks=False).st_size
    return total


def antennas(sdm) -> list[str]:
    """Antenna names in Antenna.xml, sorted; [] if the table is missing."""
    path = Path(sdm) / "Antenna.xml"
    if not path.exists():
        return []
    return sorted({r["name"] for r in _rows(path) if r.get("name")})


def parse_intents(text: str) -> list[str]:
    toks = (text or "").split()
    if len(toks) >= 2 and toks[0].isdigit() and toks[1].isdigit():
        toks = toks[2:]
    return [t.split("#", 1)[0] for t in toks]


def field_intents(sdm) -> dict:
    sdm = Path(sdm)
    names = {r["fieldId"]: r.get("fieldName", "") for r in _rows(sdm / "Field.xml")}
    scan_intents = {r["scanNumber"]: parse_intents(r.get("scanIntent", "")) for r in _rows(sdm / "Scan.xml")}
    scan_field: dict[str, set] = {}
    for r in _rows(sdm / "Main.xml"):
        scan_field.setdefault(r["scanNumber"], set()).add(r["fieldId"])
    out: dict[str, dict] = {}
    for scan, fields in scan_field.items():
        for fid in fields:
            name = names.get(fid)
            if name is None:
                raise SdmError(f"Main.xml scan {scan} names {fid}, which Field.xml does not define")
            f = out.setdefault(name, {"scans": 0, "intents": set()})
            f["scans"] += 1
            f["intents"].update(scan_intents.get(scan, []))
    return {n: {"scans": f["scans"], "intents": sorted(f["intents"])} for n, f in sorted(out.items())}
