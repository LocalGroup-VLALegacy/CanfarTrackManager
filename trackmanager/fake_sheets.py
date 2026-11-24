"""In-memory stand-in for gsheets.SheetsClient, for tests (plan §4: `FakeSheets`).

It applies the subset of batchUpdate requests that trackmanager.sheets emits (tabs, cells,
protection, spreadsheet properties) and records the rest (validation, formats, widths) without
evaluating them. Formulas are stored as their text; a test that needs an evaluated value sets it
with set_value(). values_get / values_update read and write the same cell store, so a test can
act as a reviewer by writing cells and as the bridge by reading them.
"""
from __future__ import annotations

import copy
import itertools
import re

from .gsheets import SheetsError

_A1 = re.compile(r"^(?:'((?:[^']|'')+)'|([^!]+))!?([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?$")


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeSheets:
    def __init__(self, title="fake", spreadsheet_id="fake-id", time_zone="America/Vancouver"):
        self.id = spreadsheet_id
        self.props = {"title": title, "timeZone": time_zone, "locale": "en_CA"}
        self.sheets: list[dict] = []
        self.calls = 0
        self.log: list[dict] = []          # every request applied, in order
        self._pr_ids = itertools.count(1000)
        self._add({"sheetId": 0, "title": "Sheet1", "index": 0,
                   "gridProperties": {"rowCount": 1000, "columnCount": 26}})

    # ---- internals --------------------------------------------------------------------------
    def _add(self, props):
        sh = {"properties": {"index": len(self.sheets), "gridProperties": {"rowCount": 1000, "columnCount": 26},
                             **props}, "protectedRanges": [], "cells": {}, "other": []}
        self.sheets.append(sh)
        return sh

    def sheet(self, title=None, sheet_id=None) -> dict:
        for sh in self.sheets:
            if (title is not None and sh["properties"]["title"] == title) or \
                    (sheet_id is not None and sh["properties"]["sheetId"] == sheet_id):
                return sh
        raise SheetsError(f"no sheet {title if title is not None else sheet_id!r}", 400)

    def _parse(self, rng: str):
        m = _A1.match(rng)
        if not m:
            raise SheetsError(f"bad range {rng!r}", 400)
        title = (m.group(1) or "").replace("''", "'") or m.group(2)
        sh = self.sheet(title)
        g = sh["properties"]["gridProperties"]
        c0 = _col(m.group(3)) if m.group(3) else 0
        r0 = int(m.group(4)) - 1 if m.group(4) else 0
        if m.group(5) is None and m.group(6) is None:   # single cell, or whole tab
            c1, r1 = (c0 + 1, r0 + 1) if m.group(3) else (g["columnCount"], g["rowCount"])
        else:
            c1 = _col(m.group(5)) + 1 if m.group(5) else g["columnCount"]
            r1 = int(m.group(6)) if m.group(6) else g["rowCount"]
        return sh, r0, r1, c0, c1

    def _check_grid(self, sh, r, c):
        g = sh["properties"]["gridProperties"]
        if r >= g["rowCount"] or c >= g["columnCount"]:
            raise SheetsError(f"range exceeds grid limits of {sh['properties']['title']!r} "
                              f"({g['rowCount']}x{g['columnCount']}) at row {r + 1}, column {c + 1}", 400)

    @staticmethod
    def _value(cell: dict):
        v = cell.get("userEnteredValue", {})
        for k in ("formulaValue", "stringValue", "numberValue", "boolValue"):
            if k in v:
                return v[k]
        return None

    # ---- API (same as SheetsClient) ----------------------------------------------------------
    def meta(self, fields=None) -> dict:
        self.calls += 1
        return {"spreadsheetId": self.id, "properties": dict(self.props),
                "sheets": [{"properties": copy.deepcopy(sh["properties"]),
                            **({"protectedRanges": copy.deepcopy(sh["protectedRanges"])} if sh["protectedRanges"] else {})}
                           for sh in sorted(self.sheets, key=lambda s: s["properties"]["index"])]}

    def batch_update(self, requests):
        if not requests:
            return []
        self.calls += 1
        snapshot = copy.deepcopy((self.sheets, self.props))
        try:
            return [self._apply(r) for r in requests]
        except SheetsError:
            self.sheets, self.props = snapshot   # batchUpdate is atomic
            raise

    def _apply(self, req: dict) -> dict:
        (kind, body), = req.items()
        self.log.append(copy.deepcopy(req))
        if kind == "addSheet":
            p = body["properties"]
            if any(s["properties"]["title"] == p["title"] for s in self.sheets):
                raise SheetsError(f"a sheet named {p['title']!r} already exists", 400)
            sid = p.get("sheetId", max(s["properties"]["sheetId"] for s in self.sheets) + 1)
            if any(s["properties"]["sheetId"] == sid for s in self.sheets):
                raise SheetsError(f"sheetId {sid} already exists", 400)
            sh = self._add({**copy.deepcopy(p), "sheetId": sid})
            return {"addSheet": {"properties": copy.deepcopy(sh["properties"])}}
        if kind == "deleteSheet":
            sh = self.sheet(sheet_id=body["sheetId"])
            if len(self.sheets) == 1:
                raise SheetsError("can't remove all the sheets in a document", 400)
            self.sheets.remove(sh)
            return {}
        if kind == "duplicateSheet":
            src = self.sheet(sheet_id=body["sourceSheetId"])
            title = body["newSheetName"]
            if any(s["properties"]["title"] == title for s in self.sheets):
                raise SheetsError(f"a sheet named {title!r} already exists", 400)
            sid = body.get("newSheetId", max(s["properties"]["sheetId"] for s in self.sheets) + 1)
            new = copy.deepcopy(src)
            new["properties"].update(sheetId=sid, title=title, index=len(self.sheets))
            new["properties"].pop("hidden", None)
            for pr in new["protectedRanges"]:
                pr["protectedRangeId"] = next(self._pr_ids)
                pr["range"]["sheetId"] = sid
            self.sheets.append(new)
            return {"duplicateSheet": {"properties": copy.deepcopy(new["properties"])}}
        if kind == "updateSheetProperties":
            p = body["properties"]
            sh = self.sheet(sheet_id=p["sheetId"])
            for f in body["fields"].replace("gridProperties(", "gridProperties.").rstrip(")").split(","):
                f = f.strip()
                if f.startswith("gridProperties.") or f in ("rowCount", "columnCount", "frozenRowCount",
                                                             "frozenColumnCount"):
                    key = f.split(".")[-1]
                    if key in p.get("gridProperties", {}):
                        sh["properties"]["gridProperties"][key] = p["gridProperties"][key]
                elif f in p:
                    sh["properties"][f] = p[f]
            return {}
        if kind == "updateSpreadsheetProperties":
            for f in body["fields"].split(","):
                if f in body["properties"]:
                    self.props[f] = body["properties"][f]
            return {}
        if kind == "updateCells":
            sh = self.sheet(sheet_id=body["start"]["sheetId"])
            r0, c0 = body["start"].get("rowIndex", 0), body["start"].get("columnIndex", 0)
            for i, row in enumerate(body["rows"]):
                for j, cell in enumerate(row.get("values", [])):
                    self._check_grid(sh, r0 + i, c0 + j)
                    if "userEnteredValue" in cell and "userEnteredValue" in body["fields"]:
                        sh["cells"][(r0 + i, c0 + j)] = self._value(cell)
            return {}
        if kind == "addProtectedRange":
            pr = copy.deepcopy(body["protectedRange"])
            pr["protectedRangeId"] = next(self._pr_ids)
            self.sheet(sheet_id=pr["range"]["sheetId"])["protectedRanges"].append(pr)
            return {"addProtectedRange": {"protectedRange": copy.deepcopy(pr)}}
        if kind == "deleteProtectedRange":
            for sh in self.sheets:
                sh["protectedRanges"] = [p for p in sh["protectedRanges"]
                                         if p["protectedRangeId"] != body["protectedRangeId"]]
            return {}
        if kind in ("setDataValidation", "addConditionalFormatRule", "updateDimensionProperties",
                    "repeatCell", "updateBorders"):
            rng = body.get("range") or (body.get("rule", {}).get("ranges") or [{}])[0]
            sid = rng.get("sheetId")
            if sid is not None:
                sh = self.sheet(sheet_id=sid)
                if rng.get("endRowIndex") and body.get("range") and kind != "updateDimensionProperties":
                    self._check_grid(sh, rng["endRowIndex"] - 1, rng.get("endColumnIndex", 1) - 1)
                sh["other"].append({kind: copy.deepcopy(body)})
                if kind == "updateDimensionProperties" and body["properties"].get("hiddenByUser") is not None \
                        and body["range"]["dimension"] == "COLUMNS":
                    hidden = sh["properties"].setdefault("hiddenColumns", set())
                    for c in range(body["range"]["startIndex"], body["range"]["endIndex"]):
                        (hidden.add if body["properties"]["hiddenByUser"] else hidden.discard)(c)
            return {}
        raise SheetsError(f"FakeSheets: unsupported request {kind}", 400)

    def values_get(self, ranges, render="FORMATTED_VALUE"):
        self.calls += 1
        out = []
        for rng in ranges:
            sh, r0, r1, c0, c1 = self._parse(rng)
            rows = [[sh["cells"].get((r, c), "") for c in range(c0, c1)] for r in range(r0, r1)]
            rows = [[("" if v is None else v) for v in row] for row in rows]
            for row in rows:                      # the API drops trailing empty cells and rows
                while row and row[-1] == "":
                    row.pop()
            while rows and not rows[-1]:
                rows.pop()
            if render == "FORMATTED_VALUE":
                rows = [[("TRUE" if v else "FALSE") if isinstance(v, bool) else
                         (v if isinstance(v, str) else str(v)) for v in row] for row in rows]
            out.append(rows)
        return out

    def values_update(self, data, input="USER_ENTERED"):
        if not data:
            return {}
        self.calls += 1
        for rng, values in data:
            sh, r0, _, c0, _ = self._parse(rng)
            for i, row in enumerate(values):
                for j, v in enumerate(row):
                    self._check_grid(sh, r0 + i, c0 + j)
                    if input == "USER_ENTERED" and isinstance(v, str) and v.upper() in ("TRUE", "FALSE"):
                        v = v.upper() == "TRUE"
                    sh["cells"][(r0 + i, c0 + j)] = v
        return {}

    # ---- test helpers -----------------------------------------------------------------------
    def set_value(self, title, a1cell, value):
        """Write one cell as a person (or an evaluated formula) would."""
        self.values_update([(f"'{title}'!{a1cell}", [[value]])], input="RAW")
        self.calls -= 1

    def get_value(self, title, a1cell):
        sh, r0, _, c0, _ = self._parse(f"'{title}'!{a1cell}")
        return sh["cells"].get((r0, c0))
