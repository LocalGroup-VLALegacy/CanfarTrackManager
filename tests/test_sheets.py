"""Offline tests for the Sheets spec, request builder and flag-command rebuild (no Google access)."""
import sys
from pathlib import Path

import pytest

SKEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKEL))
from trackmanager import flagcmds, sheets  # noqa: E402
from trackmanager.sheets_samples import IC10_ARCHIVE, PROJECT, samples  # noqa: E402

EDITORS = ["controller@example.iam.gserviceaccount.com", "operator@example.org"]


@pytest.fixture(scope="module")
def spec():
    return sheets.load_spec(sheets.SPEC, PROJECT)


def test_spec_placeholders_filled(spec):
    assert spec["tracker"]["title"] == "VVINGS Tracks"
    subj = sheets.tab(spec, "QA Sign-up")["columns"]
    body = next(c for c in subj if c["name"] == "Email Body")["formula"]
    assert "https://example.org/qa-guide" in body and "${" not in body
    with pytest.raises(KeyError):
        sheets.load_spec(sheets.SPEC, {"project": {"name": "x"}})  # missing review.email_prefix


def test_column_letters():
    assert [sheets.col_letter(i) for i in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"]
    assert all(sheets.col_index(sheets.col_letter(i)) == i for i in range(80))


def test_formulas_use_header_letters(spec):
    t = sheets.tab(spec, "QA Sign-up")
    lm = sheets.letters(t["columns"])
    subj = sheets.fill_formula(next(c for c in t["columns"] if c["name"] == "Email Subject")["formula"],
                               t["columns"], 7)
    assert f'{lm["Track Name"]}7<>""' in subj and f'{lm["Stage"]}7="AWAITING_REVIEW"' in subj
    assert "[VVINGS-QA] QA ready for" in subj and "{{" not in subj and "\n" not in subj


def test_signup_protection_leaves_only_reviewer_columns(spec):
    reqs = sheets.tracker_tab_requests(spec, "QA Sign-up", 11, EDITORS)
    prot = [r["addProtectedRange"]["protectedRange"] for r in reqs if "addProtectedRange" in r]
    assert len(prot) == 1 and prot[0]["range"] == {"sheetId": 11}
    assert prot[0]["editors"]["users"] == EDITORS and prot[0]["warningOnly"] is False
    t = sheets.tab(spec, "QA Sign-up")
    lm = sheets.letters(t["columns"])
    cols = sorted(u["startColumnIndex"] for u in prot[0]["unprotectedRanges"])
    assert cols == sorted(sheets.col_index(lm[n]) for n in ("QA Reviewer", "Email", "Notes"))
    assert all(u["startRowIndex"] == 1 for u in prot[0]["unprotectedRanges"])  # header row stays locked
    email = [r for r in reqs if "setDataValidation" in r]
    assert email[0]["setDataValidation"]["rule"]["condition"]["type"] == "TEXT_IS_EMAIL"


def test_track_summary_fully_protected_and_coloured(spec):
    reqs = sheets.tracker_tab_requests(spec, "Track Summary", 10, EDITORS)
    prot = [r["addProtectedRange"]["protectedRange"] for r in reqs if "addProtectedRange" in r][0]
    assert prot["unprotectedRanges"] == []
    rules = [r["addConditionalFormatRule"] for r in reqs if "addConditionalFormatRule" in r]
    assert len(rules) == len(spec["stage_colors"]) + 1            # + the HOLD rule on the Stage cell
    lm = sheets.letters(sheets.tab(spec, "Track Summary")["columns"])
    hold = rules[0]
    assert hold["index"] == 0 and hold["rule"]["ranges"][0]["startColumnIndex"] == sheets.col_index(lm["Stage"])
    assert f'LEFT(${lm["Detail"]}2, 5)="HOLD:"' in hold["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
    assert [r["index"] for r in rules] == list(range(len(rules)))
    grid = reqs[0]["updateSheetProperties"]["properties"]["gridProperties"]
    assert grid["rowCount"] == sheets.TRACKER_ROWS and grid["columnCount"] >= len(lm)


def test_tracker_row_never_overwrites_reviewer_cells(spec):
    t = sheets.tab(spec, "QA Sign-up")
    ctx = samples()[0]
    new = sheets.tracker_row(t, ctx, 2)
    again = sheets.tracker_row(t, ctx, 2, existing={"QA Reviewer": "someone"})
    assert new["QA Reviewer"] == "" and "QA Reviewer" not in again and "Email" not in again
    assert "Notified iter" not in new  # the script's column
    assert new["EBID"] == "42455346" and new["Stage"] == "AWAITING_REVIEW"
    assert new["QA Server Links"].startswith('=HYPERLINK("https://ws-uv.canfar.net/arc/files/')
    ts = sheets.tracker_row(sheets.tab(spec, "Track Summary"), ctx, 2)
    assert ts["Fields"] == "3C48, IC10_1_CTR, J0102+5824" and set(ts) == {c["name"] for c in sheets.tab(spec, "Track Summary")["columns"]}


def test_flag_template_requests(spec):
    reqs = sheets.flag_template_requests(spec, 5, EDITORS)
    prot = [r["addProtectedRange"]["protectedRange"] for r in reqs if "addProtectedRange" in r][0]
    assert prot["unprotectedRanges"] == []                      # TEMPLATE itself is fully locked ...
    assert reqs[0]["updateSheetProperties"]["properties"]["hidden"] is True   # ... and hidden
    copy = sheets.flag_tab_protection_requests(spec, 77, "D_x", [5, 6], EDITORS)   # ... each copy gets the ranges
    assert copy[:2] == [{"deleteProtectedRange": {"protectedRangeId": 5}}, {"deleteProtectedRange": {"protectedRangeId": 6}}]
    prot = copy[2]["addProtectedRange"]["protectedRange"]
    un = {(u["startRowIndex"], u["endRowIndex"], u["startColumnIndex"], u["endColumnIndex"]) for u in prot["unprotectedRanges"]}
    assert un == {(6, 150, 3, 15), (0, 1, 12, 13), (0, 1, 14, 15)}  # D7:O150, M1, O1
    assert prot["range"] == {"sheetId": 77}
    verdict = [r["setDataValidation"] for r in reqs if "setDataValidation" in r
               and r["setDataValidation"]["range"]["startColumnIndex"] == 12 and r["setDataValidation"]["range"]["startRowIndex"] == 0]
    assert [v["userEnteredValue"] for v in verdict[0]["rule"]["condition"]["values"]] == \
        ["COMPLETE", "RESTART", "MANUAL REVIEW", "HELP REQUESTED"]
    body = [r["updateCells"] for r in reqs if "updateCells" in r and r["updateCells"]["start"]["rowIndex"] == 5][0]
    assert len(body["rows"]) == 150 - 6 + 1
    row7 = body["rows"][1]["values"]
    flag = row7[sheets.col_index("R")]["userEnteredValue"]["formulaValue"]
    assert flag.startswith('=IF(Q7="", "", "mode=\'manual\'"') and "{{" not in flag
    assert row7[sheets.col_index("D")]["userEnteredValue"] == {"boolValue": False}


def test_flag_tab_name_and_cells(spec):
    ctx = samples()[0]
    assert sheets.flag_tab_name(spec, IC10_ARCHIVE) == "D_20A-346_59786_42455346"
    cells = sheets.flag_tab_cells(spec, ctx)
    assert cells["E1"] == IC10_ARCHIVE["sdm"] and cells["E4"] == "D" and cells["H1"] == 2
    assert [cells[f"V{i}"] for i in (1, 2, 3)] == IC10_ARCHIVE["fields"]
    assert "M1" not in cells and "O1" not in cells  # reviewer cells untouched


def test_preview_xlsx(spec, tmp_path):
    pytest.importorskip("openpyxl")
    out = sheets.preview_xlsx(spec, tmp_path / "p.xlsx", samples())
    import openpyxl
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames[:3] == ["Track Summary", "QA Sign-up", "D_20A-346_59786_42455346"]


# ---- flag commands ----------------------------------------------------------------------------
def row(**kw):
    base = {"Apply Row": True, "Issue Description": "noisy", "Name of QA person": "R A"}
    return {**base, **{k.replace("Date_Time", "Date/Time").replace("_", " "): v for k, v in kw.items()}}


def test_command_format_matches_template_order():
    r = {"Apply Row": True, "Issue Description": "RFI", "Name of QA person": "R A", "Field Name": "3C48",
         "Scan": "3,4", "SPW": "7:0~14", "Antenna Name": "ea10&ea12", "Start Date/Time": "2022/07/26/09:00:00",
         "End Date/Time": "2022/07/26/09:05:00", "UVrange": "0~20klambda", "Correlation": "LL"}
    assert flagcmds.command(r) == ("mode='manual' field='3C48' scan='3,4' spw='7:0~14' antenna='ea10&ea12' "
                                  "timerange='2022/07/26/09:00:00~2022/07/26/09:05:00' uvrange='0~20klambda' "
                                  "correlation='LL' reason='[MANUAL] Issue: RFI. Flagged by R A'")


def test_quotes_and_wildcards_and_blank_field():
    r = row(Issue_Description="ea10's \"bad\" scan", Field_Name="*", Scan="5")
    c = flagcmds.command(r)
    assert "field=" not in c and "reason='[MANUAL] Issue: ea10s bad scan. Flagged by R A'" in c
    assert c.count("'") % 2 == 0
    cmds, probs = flagcmds.build_commands([row(Field_Name="IC10*", Scan="6"), row(Field_Name="", Scan="5"),
                                           row(Field_Name="J0102+5824", Scan=5.0)], IC10_ARCHIVE)
    assert probs == [] and len(cmds) == 3 and "scan='5'" in cmds[2]


def test_parentheses_field_names_are_exact_matches():
    arch = {**IC10_ARCHIVE, "fields": ["3C48", "IC10_1(CTR)"]}
    assert flagcmds.check_row(row(Field_Name="IC10_1(CTR)"), arch) == []
    assert flagcmds.check_row(row(Field_Name="IC10_1"), arch)


@pytest.mark.parametrize("kw,msg", [
    (dict(Field_Name="NGC6822"), "not in this track"),
    (dict(Field_Name="M31*"), "matches no field"),
    (dict(Scan="9"), "not in this track"),
    (dict(Scan="5-7"), "use 5, 5,6 or 5~10"),
    (dict(SPW="40"), "spws 0-31"),
    (dict(Antenna_Name="ea20"), "not in this track"),
    (dict(Antenna_Name="10"), "use ea13"),
    (dict(Start_Date_Time="2022/07/26/09:00:00.5"), "no decimals"),
    (dict(Start_Date_Time="2022/07/26/10:00:00", End_Date_Time="2022/07/26/09:00:00"), "after end"),
    (dict(Start_Date_Time="2022/07/27/09:00:00"), "outside the track"),
    (dict(UVrange="lots"), "uvrange"),
    (dict(Correlation="XX"), "correlation"),
    (dict(), "whole track"),
])
def test_bad_rows_are_reported(kw, msg):
    probs = flagcmds.check_row(row(**kw), IC10_ARCHIVE)
    assert probs and any(msg in p for p in probs), probs


def test_unticked_rows_ignored_and_problems_have_sheet_rows():
    rows = [row(Scan="5", Apply_Row=False), row(Scan="99"), row(Scan="5", Apply_Row="TRUE")]
    cmds, probs = flagcmds.build_commands(rows, IC10_ARCHIVE)
    assert len(cmds) == 1 and [p[0] for p in probs] == [8]
    text = flagcmds.flag_file(IC10_ARCHIVE["sdm"], "D", cmds)
    assert text.splitlines()[:2] == [f"# Manual flagging for track {IC10_ARCHIVE['sdm']}", "# Config: D"]


def test_protection_editors_default_and_list():
    sa = "controller@example.iam.gserviceaccount.com"
    assert sheets.protection_editors({}, sa) == [sa, "koch.eric.w@gmail.com"]
    proj = {"sheets": {"editors": ["koch.eric.w@gmail.com", "second.op@example.org", "KOCH.eric.w@gmail.com"]}}
    assert sheets.protection_editors(proj, sa) == [sa, "koch.eric.w@gmail.com", "second.op@example.org"]
    assert sheets.protection_editors({"sheets": {"editors": []}}, sa) == [sa]  # explicit empty: service account only
    with pytest.raises(ValueError):
        sheets.protection_editors({"sheets": {"editors": ["not-an-email"]}}, sa)
    reqs = sheets.tracker_tab_requests(sheets.load_spec(sheets.SPEC, PROJECT), "QA Sign-up", 1,
                                       sheets.protection_editors(PROJECT, sa))
    users = [r for r in reqs if "addProtectedRange" in r][0]["addProtectedRange"]["protectedRange"]["editors"]["users"]
    assert users == [sa, "koch.eric.w@gmail.com"]


def test_qa_home_and_history_links(spec):
    home = PROJECT["qa"]["home_url"]
    for name in ("Track Summary", "QA Sign-up"):
        t = sheets.tab(spec, name)
        reqs = sheets.tracker_tab_requests(spec, name, 1, EDITORS)
        header = reqs[1]["updateCells"]["rows"][0]["values"]
        linked = [v["userEnteredValue"]["stringValue"] for v in header
                  if v["userEnteredFormat"]["textFormat"].get("link", {}).get("uri") == home]
        assert linked == ["QA link" if name == "Track Summary" else "QA Server Links"]
        row = sheets.tracker_row(t, samples()[0], 2)
        assert row["QA history"].startswith('=HYPERLINK("https://ws-uv.canfar.net/arc/files/') \
            and row["QA history"].endswith('"all iterations")')
    body = sheets.tab(spec, "QA Sign-up")["columns"]
    formula = next(c for c in body if c["name"] == "Email Body")["formula"]
    # the body joins the plain-URL columns: a HYPERLINK cell would give its label ("QA"), not the address
    assert all(f"{{{{{n}}}}}" in formula for n in ("QA URL", "History URL", "Flags URL"))
    assert "{{QA Server Links}}" not in formula and "{{QA history}}" not in formula
    row = sheets.tracker_row(sheets.tab(spec, "QA Sign-up"), samples()[0], 2)
    assert row["QA URL"] == samples()[0]["links"]["qa"] and not row["QA URL"].startswith("=")
    cells = sheets.flag_tab_cells(spec, samples()[0])
    assert cells["H3"].endswith('"QA history")')
    j3 = next(h for h in spec["flagbook"]["header_cells"] if h.get("cell") == "J3")
    assert home in j3["formula"]
