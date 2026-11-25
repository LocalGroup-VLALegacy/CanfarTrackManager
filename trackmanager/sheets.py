"""Sheets template builder (plan §3.5): spec -> Sheets API requests, row values, xlsx preview.

Nothing here talks to Google. It turns templates/sheets/spec_v1.yaml into
  - batchUpdate request bodies that create a tab with its headers, widths, validation,
    conditional formats and protected ranges (tracker_tab_requests, flag_template_requests);
  - the cell values for one track's row or flag tab, by header name (tracker_row, flag_tab_cells);
  - a local .xlsx preview to look at the layout (preview_xlsx; needs openpyxl).
The Sheets bridge will send these with gspread; FakeSheets tests can check them offline.

    python -m trackmanager.sheets --project-name VVINGS --out preview.xlsx
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

SPEC = Path(__file__).resolve().parent / "templates" / "sheets" / "spec_v1.yaml"
TRACKER_ROWS = 5000        # grid rows of a tracker tab; validation and reviewer ranges reach this row


# ---- spec loading --------------------------------------------------------------------------
def load_spec(path=SPEC, project: dict | None = None) -> dict:
    """Read the spec and fill ${project.x} placeholders from a project config dict."""
    text = Path(path).read_text()
    project = project or {}

    def lookup(m):
        cur = project
        for part in m.group(1).split("."):
            if not isinstance(cur, dict) or part not in cur:
                raise KeyError(f"spec needs ${{{m.group(1)}}} but the project config has no such value")
            cur = cur[part]
        return str(cur)

    raw = yaml.safe_load(text)
    # Substitute after parsing, so values containing YAML-special characters are safe.
    def walk(o):
        if isinstance(o, str):
            return re.sub(r"\$\{([a-z_.]+)\}", lookup, o)
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o
    return walk(raw)


def col_letter(i: int) -> str:
    """0-based column index -> A, B, ..., Z, AA, ..."""
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def col_index(letter: str) -> int:
    n = 0
    for ch in letter:
        n = n * 26 + ord(ch) - 64
    return n - 1


def letters(columns: list[dict]) -> dict[str, str]:
    """header -> column letter (explicit "col", else position)."""
    return {c["name"]: c.get("col") or col_letter(i) for i, c in enumerate(columns)}


def fill_formula(formula: str, columns: list[dict], row: int) -> str:
    """Replace {{Header}} with <letter><row> and {{Header|col}} with <letter>."""
    lmap = letters(columns)

    def sub(m):
        name, _, mod = m.group(1).partition("|")
        if name not in lmap:
            raise KeyError(f"formula refers to unknown column {name!r}")
        return lmap[name] if mod == "col" else f"{lmap[name]}{row}"
    out = re.sub(r"\{\{([^}]+)\}\}", sub, formula).replace("{r}", str(row))
    return " ".join(out.split())  # YAML folding leaves newlines; Sheets doesn't need them


def tab(spec: dict, name: str) -> dict:
    return next(t for t in spec["tracker"]["tabs"] if t["name"] == name)


# ---- values for one track -------------------------------------------------------------------
def _get(ctx: dict, source: str):
    cur = ctx
    for part in source.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _cell(value, col: dict):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    if col.get("link_text") and isinstance(value, str) and value:
        return f'=HYPERLINK("{value}", "{col["link_text"]}")'
    return value


def tracker_row(tab_spec: dict, ctx: dict, row: int, existing: dict | None = None) -> dict:
    """Header -> value for one track's row. Reviewer cells are never written once the row
    exists (they belong to the reviewer); script cells are left alone."""
    out = {}
    for c in tab_spec["columns"]:
        src = c["source"]
        if src == "formula":
            out[c["name"]] = fill_formula(c["formula"], tab_spec["columns"], row)
        elif src == "script":
            continue
        elif src == "reviewer":
            if existing is None:
                out[c["name"]] = ""
        else:
            out[c["name"]] = _cell(_get(ctx, src), c)
    return out


def flag_tab_name(spec: dict, archive: dict) -> str:
    """<config>_<project>_<mjd>_<ebid>. An EB with no EB id (TAP names a few
    `<project>_sb<sbid>_<n>_<m>.<mjd>`) or a track with no archive record uses its SDM or track
    name instead, so every track gets a distinct tab name."""
    try:
        if not archive.get("ebid"):
            raise KeyError("ebid")
        return spec["flagbook"]["tab_name"].format(config=archive["config"], project=archive["project"],
                                                   mjd=int(float(archive["mjd"])), ebid=archive["ebid"])
    except (KeyError, TypeError, ValueError):
        name = str(archive.get("sdm") or archive.get("eb") or "track")
        return (f"{archive['config']}_" if archive.get("config") else "") + name[-90:]


def flag_tab_cells(spec: dict, ctx: dict) -> dict:
    """A1-notation -> value for a new flag tab copied from TEMPLATE: header values and the
    hidden list of field names used by the Field Name dropdown."""
    fb = spec["flagbook"]
    out = {}
    for h in fb["header_cells"]:
        if "value" in h and h.get("source") not in (None, "reviewer"):
            out[h["value"]] = _cell(_get(ctx, h["source"]), h)
    start = re.match(r"([A-Z]+)(\d+)", fb["table"]["hidden_range"].split(":")[0])
    for i, field in enumerate(ctx.get("archive", {}).get("fields", [])):
        out[f"{start.group(1)}{int(start.group(2)) + i}"] = field
    return out


# ---- who may edit protected ranges --------------------------------------------------------------
DEFAULT_EDITORS = ["koch.eric.w@gmail.com"]   # used when a project config lists none
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def protection_editors(project: dict, service_account: str) -> list[str]:
    """Editors of every protected range: the controller's service account, then the human
    editors from project.yaml `sheets.editors` (a list, so more can be added per project),
    or DEFAULT_EDITORS when that key is missing. Duplicates are dropped; bad addresses raise."""
    humans = (project.get("sheets") or {}).get("editors")
    if humans is None:
        humans = DEFAULT_EDITORS
    if isinstance(humans, str):
        humans = [humans]
    out = []
    for e in [service_account, *humans]:
        e = e.strip()
        if not EMAIL_RE.match(e):
            raise ValueError(f"not an email address in sheets.editors: {e!r}")
        if e.lower() not in (x.lower() for x in out):
            out.append(e)
    return out


# ---- Sheets API requests ----------------------------------------------------------------------
def _rgb(hexcol: str) -> dict:
    h = hexcol.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def _grid(sheet_id, a1: str) -> dict:
    """A1 range ('D7:O150', 'M1', 'V1:V200') -> GridRange (0-based, end-exclusive)."""
    m = re.fullmatch(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", a1)
    c0, r0 = col_index(m.group(1)), int(m.group(2)) - 1
    c1 = col_index(m.group(3)) + 1 if m.group(3) else c0 + 1
    r1 = int(m.group(4)) if m.group(4) else r0 + 1
    return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1}


def _validation(sheet_id, a1, v: dict) -> dict:
    t = v["type"]
    if t == "ONE_OF_LIST":
        cond = {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": str(x)} for x in v["values"]]}
    elif t == "ONE_OF_RANGE":
        cond = {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": v["range"]}]}
    elif t == "CUSTOM_FORMULA":
        cond = {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": v["formula"]}]}
    elif t in ("BOOLEAN", "TEXT_IS_EMAIL"):
        cond = {"type": t}
    else:
        raise ValueError(f"unsupported validation type {t}")
    rule = {"condition": cond, "strict": bool(v.get("strict", t == "BOOLEAN")), "showCustomUi": True}
    if v.get("help"):
        rule["inputMessage"] = v["help"]
    return {"setDataValidation": {"range": _grid(sheet_id, a1), "rule": rule}}


def _protect(sheet_id, title, editable: list[str], editors: list[str]) -> dict:
    return {"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": sheet_id},
        "description": f"{title}: managed by the VLA calibration controller (spec v1)",
        "warningOnly": False,
        "editors": {"users": list(editors)},
        "unprotectedRanges": [_grid(sheet_id, a1) for a1 in editable]}}}


def _header_cells(sheet_id, row0: int, names: list[str], col0: int = 0,
                  links: dict | None = None) -> dict:
    """Bold grey header cells. links: header name -> URL, applied as a link on the header's text,
    so the cell's value is still the plain header name."""
    links = links or {}

    def fmt(n):
        tf = {"bold": True, **({"link": {"uri": links[n]}} if links.get(n) else {})}
        return {"textFormat": tf, "wrapStrategy": "WRAP", "backgroundColor": _rgb("#d9d9d9")}
    return {"updateCells": {
        "start": {"sheetId": sheet_id, "rowIndex": row0, "columnIndex": col0},
        "rows": [{"values": [{"userEnteredValue": {"stringValue": n},
                              "userEnteredFormat": fmt(n)} for n in names]}],
        "fields": "userEnteredValue,userEnteredFormat(textFormat,wrapStrategy,backgroundColor)"}}


def _width(sheet_id, idx: int, px: int) -> dict:
    return {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": idx, "endIndex": idx + 1},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def _stage_rules(spec, sheet_id, stage_letter: str, ncols: int, first_row: int,
                 detail_letter: str | None = None) -> list[dict]:
    """Row colours by Stage. With a Detail column, a first rule (it wins over the row colours)
    paints the Stage cell in hold_color when Detail starts with "HOLD:"."""
    reqs = []
    if detail_letter and spec.get("hold_color"):
        sc = col_index(stage_letter)
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": first_row - 1,
                        "startColumnIndex": sc, "endColumnIndex": sc + 1}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                          "values": [{"userEnteredValue": f'=LEFT(${detail_letter}{first_row}, 5)="HOLD:"'}]},
                            "format": {"backgroundColor": _rgb(spec["hold_color"])}}}}})
    for i, (stage, color) in enumerate(spec["stage_colors"].items(), start=len(reqs)):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": first_row - 1,
                        "startColumnIndex": 0, "endColumnIndex": ncols}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                          "values": [{"userEnteredValue": f'=${stage_letter}{first_row}="{stage}"'}]},
                            "format": {"backgroundColor": _rgb(color)}}}}})
    return reqs


def tracker_tab_requests(spec: dict, tab_name: str, sheet_id: int, editors: list[str]) -> list[dict]:
    """Requests that turn an empty sheet (sheet_id) into the given tracker tab."""
    t = tab(spec, tab_name)
    cols, hr = t["columns"], t["header_row"]
    lmap = letters(cols)
    reqs = [{"updateSheetProperties": {"properties": {
        "sheetId": sheet_id, "title": tab_name,
        "gridProperties": {"rowCount": TRACKER_ROWS, "columnCount": max(26, len(cols)),
                           "frozenRowCount": t.get("frozen_rows", 1), "frozenColumnCount": t.get("frozen_cols", 0)}},
        "fields": "title,gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount)"}},
        _header_cells(sheet_id, hr - 1, [c["name"] for c in cols],
                      links={c["name"]: c["header_link"] for c in cols if c.get("header_link")})]
    for i, c in enumerate(cols):
        if c.get("width"):
            reqs.append(_width(sheet_id, i, c["width"]))
        if c.get("hidden"):
            reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                                 "startIndex": i, "endIndex": i + 1},
                                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
        if c.get("validation"):
            L = lmap[c["name"]]
            reqs.append(_validation(sheet_id, f"{L}{hr + 1}:{L}{TRACKER_ROWS}", c["validation"]))
        if c.get("note"):
            reqs.append({"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": hr - 1, "columnIndex": i},
                                         "rows": [{"values": [{"note": c["note"]}]}], "fields": "note"}})
    if t.get("color_by"):
        reqs += _stage_rules(spec, sheet_id, lmap[t["color_by"]], len(cols), hr + 1, lmap.get("Detail"))
    editable = [f"{lmap[n]}{hr + 1}:{lmap[n]}{TRACKER_ROWS}" for n in t["protection"]["editable_columns"]]
    reqs.append(_protect(sheet_id, tab_name, editable, editors))
    return reqs


def flag_template_requests(spec: dict, sheet_id: int, editors: list[str]) -> list[dict]:
    """Requests that turn an empty sheet into the flag-book TEMPLATE tab (copied per EB)."""
    fb, tb = spec["flagbook"], spec["flagbook"]["table"]
    cols = tb["columns"]
    reqs = [{"updateSheetProperties": {"properties": {
        "sheetId": sheet_id, "title": fb["template_tab"], "hidden": True,
        "gridProperties": {"frozenRowCount": tb["header_row"], "columnCount": 26,
                           "rowCount": max(tb["last_row"], int(re.search(r"(\d+)$", tb["hidden_range"]).group(1))) + 10}},
        "fields": "title,hidden,gridProperties(frozenRowCount,rowCount,columnCount)"}}]
    cells = {}
    for h in fb["header_cells"]:
        if "label" in h:
            cells[h["label"]] = {"userEnteredValue": {"stringValue": h["text"]},
                                 "userEnteredFormat": {"textFormat": {"bold": True}, "wrapStrategy": "WRAP"}}
        if "cell" in h:
            cells[h["cell"]] = {"userEnteredValue": {"formulaValue": h["formula"]}}
        if h.get("validation"):
            reqs.append(_validation(sheet_id, h["value"], h["validation"]))
    for a1, cell in cells.items():
        g = _grid(sheet_id, a1)
        reqs.append({"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": g["startRowIndex"],
                                               "columnIndex": g["startColumnIndex"]},
                                     "rows": [{"values": [cell]}], "fields": ",".join(
                                         ["userEnteredValue"] + (["userEnteredFormat(textFormat,wrapStrategy)"]
                                                                 if "userEnteredFormat" in cell else []))}})
    reqs.append(_header_cells(sheet_id, tb["header_row"] - 1, [c["name"] for c in cols]))
    rows = []  # example row, then the formula columns for every input row
    for r in range(tb["example_row"], tb["last_row"] + 1):
        vals = []
        for c in cols:
            if c["source"] == "formula":
                vals.append({"userEnteredValue": {"formulaValue": fill_formula(c["formula"], cols, r)}})
            elif r == tb["example_row"] and c.get("example"):
                vals.append({"userEnteredValue": {"stringValue": c["example"]},
                             "userEnteredFormat": {"textFormat": {"italic": True}}})
            elif c.get("validation", {}).get("type") == "BOOLEAN":
                vals.append({"userEnteredValue": {"boolValue": False}})
            else:
                vals.append({})
        rows.append({"values": vals})
    reqs.append({"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": tb["example_row"] - 1, "columnIndex": 0},
                                 "rows": rows, "fields": "userEnteredValue,userEnteredFormat(textFormat)"}})
    for c in cols:
        L = c["col"]
        if c.get("width"):
            reqs.append(_width(sheet_id, col_index(L), c["width"]))
        if c.get("hidden"):
            reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                                 "startIndex": col_index(L), "endIndex": col_index(L) + 1},
                                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
        if c.get("validation"):
            reqs.append(_validation(sheet_id, f"{L}{tb['first_row']}:{L}{tb['last_row']}", c["validation"]))
    hcol = col_index(tb["hidden_range"][0])
    reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                         "startIndex": hcol, "endIndex": hcol + 1},
                                               "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    for i, cf in enumerate(tb["conditional_formats"]):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [_grid(sheet_id, cf["range"])],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": cf["formula"]}]},
                            "format": {"backgroundColor": _rgb(cf["color"])}}}}})
    reqs.append(_protect(sheet_id, fb["template_tab"], [], editors))   # fully locked; copies get the reviewer ranges
    return reqs


def flag_clear_requests(spec: dict, sheet_id: int) -> list[dict]:
    """Empty the cells a track's tab must not inherit from TEMPLATE (`copy_clear_ranges`: verdict,
    avoid-refant, Added in iter, Check). The template's flag rows are kept."""
    fb = spec["flagbook"]
    bools = {c["col"] for c in fb["table"]["columns"] if c.get("validation", {}).get("type") == "BOOLEAN"}
    reqs = []
    for a1 in fb["protection"]["copy_clear_ranges"]:
        g = _grid(sheet_id, a1)
        row = [{"userEnteredValue": {"boolValue": False}} if col_letter(c) in bools else {}
               for c in range(g["startColumnIndex"], g["endColumnIndex"])]
        reqs.append({"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": g["startRowIndex"],
                                               "columnIndex": g["startColumnIndex"]},
                                     "rows": [{"values": row} for _ in range(g["startRowIndex"], g["endRowIndex"])],
                                     "fields": "userEnteredValue"}})
    return reqs


def flag_tab_copy_requests(spec: dict, template_id: int, new_id: int, title: str, index: int) -> list[dict]:
    """A track's flag tab: copy TEMPLATE (its default flag rows included), show it, and clear the
    verdict cells. Its protection is set afterwards (flag_tab_protection_requests), once the copied
    one's id is known."""
    return [{"duplicateSheet": {"sourceSheetId": template_id, "newSheetId": new_id, "newSheetName": title,
                                "insertSheetIndex": index}},
            {"updateSheetProperties": {"properties": {"sheetId": new_id, "hidden": False}, "fields": "hidden"}},
            *flag_clear_requests(spec, new_id)]


def flag_tab_protection_requests(spec: dict, sheet_id: int, title: str, protected_range_ids, editors) -> list[dict]:
    """Replace whatever protection a tab carries with the track-tab one (reviewer ranges editable)."""
    return [*({"deleteProtectedRange": {"protectedRangeId": pid}} for pid in protected_range_ids),
            _protect(sheet_id, title, spec["flagbook"]["protection"]["editable_ranges"], editors)]


def template_input(spec: dict, grid) -> list[str]:
    """Cells of TEMPLATE (a values_get grid of A1:T<last_row>) that hold something a track's tab must
    not inherit: a verdict, an avoid-refant list, Added in iter or Check. Flag rows are expected."""
    fb = spec["flagbook"]
    out = []
    for a1 in fb["protection"]["copy_clear_ranges"]:
        g = _grid(0, a1)
        for r in range(g["startRowIndex"], g["endRowIndex"]):
            for c in range(g["startColumnIndex"], g["endColumnIndex"]):
                v = grid[r][c] if r < len(grid) and c < len(grid[r]) else ""
                if v not in ("", None, False) and str(v).upper() != "FALSE":
                    out.append(f"{col_letter(c)}{r + 1}")
    return out


# ---- local preview ----------------------------------------------------------------------------
def preview_xlsx(spec: dict, out: Path, samples: list[dict]) -> Path:
    """Write an .xlsx with both tracker tabs (sample rows) and one flag tab, to look at the layout.
    Approximations: Sheets-only functions (REGEXMATCH, REGEXEXTRACT) are left as text in
    conditional formats and validation; protection is shown as Excel sheet protection with the
    reviewer cells unlocked."""
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Font, PatternFill, Protection
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    wb.remove(wb.active)
    bold = Font(bold=True)
    grey = PatternFill("solid", fgColor="D9D9D9")
    excel_ok = lambda f: not re.search(r"REGEX|TO_TEXT", f)

    for t in spec["tracker"]["tabs"]:
        ws = wb.create_sheet(t["name"])
        cols = t["columns"]
        lmap = letters(cols)
        for i, c in enumerate(cols):
            cell = ws.cell(row=1, column=i + 1, value=c["name"])
            cell.font, cell.fill = bold, grey
            cell.alignment = Alignment(wrap_text=True)
            ws.column_dimensions[col_letter(i)].width = max(8, c.get("width", 100) / 7)
        for k, ctx in enumerate(samples):
            r = k + 2
            vals = tracker_row(t, ctx, r)
            for i, c in enumerate(cols):
                v = vals.get(c["name"], ctx.get("signup", {}).get(c["name"], ""))
                if c["source"] == "reviewer":
                    v = ctx.get("signup", {}).get(c["name"], "")
                ws.cell(row=r, column=i + 1, value=v)
        last = len(samples) + 50
        for n in t["protection"]["editable_columns"]:
            for r in range(2, last):
                ws[f"{lmap[n]}{r}"].protection = Protection(locked=False)
        ws.protection.sheet = True
        stage = lmap[t["color_by"]]
        for stg, color in spec["stage_colors"].items():
            ws.conditional_formatting.add(f"A2:{col_letter(len(cols) - 1)}{last}",
                                          FormulaRule(formula=[f'${stage}2="{stg}"'],
                                                      fill=PatternFill("solid", bgColor=color.lstrip("#"))))
        for c in cols:
            if c.get("validation", {}).get("type") == "TEXT_IS_EMAIL":
                dv = DataValidation(type="custom", formula1=f'ISNUMBER(FIND("@",{lmap[c["name"]]}2))', allow_blank=True)
                dv.add(f"{lmap[c['name']]}2:{lmap[c['name']]}{last}")
                ws.add_data_validation(dv)
        ws.freeze_panes = ws.cell(row=t.get("frozen_rows", 1) + 1, column=t.get("frozen_cols", 0) + 1)

    fb, tb = spec["flagbook"], spec["flagbook"]["table"]
    ctx = samples[0]
    ws = wb.create_sheet(flag_tab_name(spec, ctx["archive"])[:31])
    cols = tb["columns"]
    for h in fb["header_cells"]:
        if "label" in h:
            ws[h["label"]] = h["text"]
            ws[h["label"]].font = bold
            ws[h["label"]].alignment = Alignment(wrap_text=True)
        if "cell" in h:
            ws[h["cell"]] = h["formula"]
    for a1, v in flag_tab_cells(spec, ctx).items():
        ws[a1] = v
    for c in cols:
        cell = ws[f"{c['col']}{tb['header_row']}"]
        cell.value, cell.font, cell.fill = c["name"], bold, grey
        cell.alignment = Alignment(wrap_text=True)
        ws.column_dimensions[c["col"]].width = max(8, c.get("width", 100) / 7)
        if c.get("hidden"):
            ws.column_dimensions[c["col"]].hidden = True
    ws.column_dimensions[tb["hidden_range"][0]].hidden = True
    for r in range(tb["example_row"], tb["last_row"] + 1):
        for c in cols:
            if c["source"] == "formula":
                f = fill_formula(c["formula"], cols, r)
                ws[f"{c['col']}{r}"] = f if excel_ok(f) else ""
            elif r == tb["example_row"] and c.get("example"):
                ws[f"{c['col']}{r}"] = c["example"]
            elif c.get("validation", {}).get("type") == "BOOLEAN":
                ws[f"{c['col']}{r}"] = False
    for k, row in enumerate(ctx.get("flag_rows", [])):  # sample reviewer rows
        r = tb["first_row"] + k
        for name, v in row.items():
            col = next(c["col"] for c in cols if c["name"] == name)
            ws[f"{col}{r}"] = v
    for a1 in fb["protection"]["editable_ranges"]:
        for row in ws[a1] if ":" in a1 else [[ws[a1]]]:
            for cell in row:
                cell.protection = Protection(locked=False)
    ws.protection.sheet = True
    dv = DataValidation(type="list", formula1='"COMPLETE,RESTART,MANUAL REVIEW,HELP REQUESTED"')
    dv.add("M1"); ws.add_data_validation(dv)
    dv = DataValidation(type="list", formula1="$V$1:$V$200", showErrorMessage=False)
    dv.add(f"F{tb['first_row']}:F{tb['last_row']}"); ws.add_data_validation(dv)
    dv = DataValidation(type="list", formula1='"TRUE,FALSE"')
    dv.add(f"D{tb['first_row']}:D{tb['last_row']}"); ws.add_data_validation(dv)
    for cf in tb["conditional_formats"]:
        if excel_ok(cf["formula"]):
            ws.conditional_formatting.add(cf["range"], FormulaRule(formula=[cf["formula"].lstrip("=")],
                                                                   fill=PatternFill("solid", bgColor=cf["color"].lstrip("#"))))
    ws.freeze_panes = f"A{tb['header_row'] + 1}"
    readme = next((t for t in fb.get("extra_tabs", []) if t["name"] == "README"), None)
    if readme:
        wr = wb.create_sheet("README (flag book)")
        for i, line in enumerate(readme["text"].splitlines(), 1):
            wr.cell(row=i, column=1, value=line)
        wr.column_dimensions["A"].width = 120
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return Path(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spec", default=str(SPEC))
    p.add_argument("--project-name", default="VVINGS")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    from .sheets_samples import PROJECT, samples
    spec = load_spec(a.spec, {**PROJECT, "project": {**PROJECT["project"], "name": a.project_name}})
    print(f"wrote {preview_xlsx(spec, Path(a.out), samples())}")


if __name__ == "__main__":
    main()
