"""M4 step 1-2: the Google client (mocked transport), FakeSheets, the tab-structure builder,
protection drift checks, the sheets config and `init --sheets` (no Google access)."""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from trackmanager import config, gsheets, init, sheets, sheets_setup  # noqa: E402
from trackmanager.fake_sheets import FakeDrive, FakeSheets  # noqa: E402
from trackmanager.store import Root, iso, write_json  # noqa: E402

SA = "controller@example.iam.gserviceaccount.com"


def write_key(path, mode=0o600, **over):
    path.write_text(json.dumps({"type": "service_account", "client_email": SA, "project_id": "p",
                                "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
                                **over}))
    os.chmod(path, mode)
    return path


def project(tmp_path, **sheets_over):
    key = write_key(tmp_path / "key.json")
    data = {"project": {"name": "VVINGS-test", "root": str(tmp_path / "run")},
            "review": {"email_prefix": "[VVINGS-QA]"},
            "qa": {"products_dir": "/arc/projects/VVINGS/calibrated/products"},
            "sheets": {"enabled": True, "tracker_id": "T", "flagbook_id": "F", "flags_folder_id": "FOLDER",
                       "credentials": str(key),
                       "editors": ["koch.eric.w@gmail.com"],
                       "links": {"qa_guide": "https://example.org/g", "flag_rows_help": "https://example.org/h"},
                       **sheets_over}}
    p = tmp_path / "project.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


# ---- key file and client ---------------------------------------------------------------------
def test_check_key(tmp_path):
    k = write_key(tmp_path / "k.json")
    assert gsheets.check_key(k) == {"client_email": SA, "project_id": "p"}
    os.chmod(k, 0o640)
    with pytest.raises(gsheets.KeyFileError, match="chmod 600"):
        gsheets.check_key(k)
    with pytest.raises(gsheets.KeyFileError, match="not a service-account key"):
        gsheets.check_key(write_key(tmp_path / "u.json", type="authorized_user"))
    with pytest.raises(gsheets.KeyFileError):
        gsheets.check_key(tmp_path / "missing.json")


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class Session:
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def request(self, method, url, **kw):
        self.seen.append((method, url, kw))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_client_retries_429_and_connection_errors_then_succeeds():
    s = Session([Resp(429, {"error": {"message": "quota"}}), ConnectionError("reset"),
                 Resp(200, {"replies": [{}, {"addSheet": {}}]})])
    sleeps = []
    c = gsheets.SheetsClient("ID", session=s, sleep=sleeps.append)
    assert c.batch_update([{"a": 1}, {"b": 2}]) == [{}, {"addSheet": {}}]
    assert len(sleeps) == 2 and c.calls == 3
    method, url, kw = s.seen[-1]
    assert method == "POST" and url.endswith("/ID:batchUpdate") and kw["json"]["requests"] == [{"a": 1}, {"b": 2}]


def test_client_does_not_retry_permission_errors_and_gives_up_after_tries():
    c = gsheets.SheetsClient("ID", session=Session([Resp(403, {"error": {"message": "no access"}})]),
                             sleep=lambda s: None)
    with pytest.raises(gsheets.SheetsError, match="HTTP 403: no access") as e:
        c.meta()
    assert e.value.status == 403 and c.calls == 1
    c = gsheets.SheetsClient("ID", session=Session([Resp(503)] * 3), tries=3, sleep=lambda s: None)
    with pytest.raises(gsheets.SheetsError, match="503"):
        c.meta()
    assert c.calls == 3


def test_client_values_calls():
    s = Session([Resp(200, {"valueRanges": [{"values": [["a", "b"]]}, {}]}), Resp(200, {})])
    c = gsheets.SheetsClient("ID", session=s, sleep=lambda x: None)
    assert c.values_get(["'Tab'!A1:B1", "'Tab'!C1"]) == [[["a", "b"]], []]
    assert s.seen[0][2]["params"]["valueRenderOption"] == "FORMATTED_VALUE"
    c.values_update([("'Tab'!A2", [[1, 2]])])
    assert s.seen[1][2]["json"] == {"valueInputOption": "USER_ENTERED",
                                    "data": [{"range": "'Tab'!A2", "majorDimension": "ROWS", "values": [[1, 2]]}]}
    assert c.values_get([]) == [] and c.values_update([]) == {} and c.batch_update([]) == []
    assert gsheets.a1("it's", "A1") == "'it''s'!A1"


# ---- FakeSheets basics -------------------------------------------------------------------------
def test_fake_sheets_grid_limits_and_atomic_batches():
    f = FakeSheets()
    with pytest.raises(gsheets.SheetsError, match="grid limits"):
        f.batch_update([{"addSheet": {"properties": {"sheetId": 5, "title": "X"}}},
                        {"updateCells": {"start": {"sheetId": 5, "rowIndex": 2000, "columnIndex": 0},
                                         "rows": [{"values": [{"userEnteredValue": {"stringValue": "a"}}]}],
                                         "fields": "userEnteredValue"}}])
    assert [s["properties"]["title"] for s in f.meta()["sheets"]] == ["Sheet1"]   # nothing applied
    f.values_update([("'Sheet1'!B2", [["x", True]])])
    assert f.values_get(["'Sheet1'!A1:C3"]) == [[[], ["", "x", "TRUE"]]]


# ---- structure builder ---------------------------------------------------------------------------
@pytest.fixture
def cfg(tmp_path):
    return config.load(project(tmp_path), check_paths=False)


def test_build_creates_managed_tabs_and_checks_clean(cfg):
    for which, titles in (("tracker", ["Track Summary", "QA Sign-up", "_meta"]),
                          ("flagbook", ["README", "TEMPLATE", "_meta"])):
        f = FakeSheets()
        rep = sheets_setup.build(cfg, f, which, SA)
        assert rep["created"] == titles and rep["deleted"] == ["Sheet1"] and rep["problems"] == []
        assert [s["properties"]["title"] for s in f.meta()["sheets"]] == titles
        assert f.props["timeZone"] == sheets_setup.TIME_ZONE
        assert f.sheet("_meta")["properties"]["hidden"] is True
        assert rep["editors"] == [SA, "koch.eric.w@gmail.com"]
        assert f.get_value("_meta", "B2") == 1 and f.get_value("_meta", "B4") == SA
        before, writes = f.calls, len(f.log)
        again = sheets_setup.build(cfg, f, which, SA)      # rerun: nothing to do, nothing sent
        assert again["created"] == [] and again["existing"] == titles and again["problems"] == []
        assert len(f.log) == writes and f.calls - before <= 4  # reads only (metadata; TEMPLATE check)


def test_build_tracker_headers_hidden_url_columns_and_formulas(cfg):
    f = FakeSheets()
    sheets_setup.build(cfg, f, "tracker", SA)
    spec = sheets.load_spec(sheets.SPEC, sheets_setup.project_values(cfg))
    t = sheets.tab(spec, "QA Sign-up")
    hdr = f.values_get(["'QA Sign-up'!1:1"])[0][0]
    assert hdr == [c["name"] for c in t["columns"]]
    hidden = f.sheet("QA Sign-up")["properties"]["hiddenColumns"]
    assert hidden == {hdr.index(n) for n in ("QA URL", "History URL", "Flags URL")}
    assert f.values_get(["'Track Summary'!A1"])[0] == [["EBID"]]
    # project values: brackets stripped from email_prefix, QA home from products_dir
    subj = next(c for c in t["columns"] if c["name"] == "Email Subject")["formula"]
    assert '"[VVINGS-QA] QA ready for "' in subj
    assert spec["tracker"]["tabs"][0]["columns"][-3]["header_link"] == \
        "https://ws-uv.canfar.net/arc/files/projects/VVINGS/calibrated/products/index.html"


def test_build_flagbook_template_and_readme(cfg):
    f = FakeSheets()
    sheets_setup.build(cfg, f, "flagbook", SA)
    assert f.get_value("TEMPLATE", "R5") == "Flag string"
    assert f.get_value("TEMPLATE", "R7").startswith('=IF(Q7="", "", "mode=')
    assert f.get_value("TEMPLATE", "D7") is False
    assert f.get_value("TEMPLATE", "I2") == '=HYPERLINK("https://example.org/g", "QA Guide")'
    assert f.get_value("README", "A1").startswith("Each track has its own flag spreadsheet")


def test_build_keeps_a_nonempty_sheet1_and_refuses_a_taken_sheet_id(cfg):
    f = FakeSheets()
    f.set_value("Sheet1", "A1", "operator notes")
    rep = sheets_setup.build(cfg, f, "tracker", SA)
    assert rep["deleted"] == [] and "Sheet1" in [s["properties"]["title"] for s in f.meta()["sheets"]]
    g = FakeSheets()
    g.batch_update([{"addSheet": {"properties": {"sheetId": 101, "title": "mine"}}}])
    with pytest.raises(RuntimeError, match="sheetId 101 is taken"):
        sheets_setup.build(cfg, g, "tracker", SA)


def test_check_reports_protection_drift(cfg):
    f = FakeSheets()
    rep = sheets_setup.build(cfg, f, "flagbook", SA)
    spec = sheets.load_spec(sheets.SPEC, sheets_setup.project_values(cfg))
    tmpl = f.sheet("TEMPLATE")["protectedRanges"][0]
    tmpl["editors"]["users"].append("intruder@example.org")
    tmpl["unprotectedRanges"] = [sheets._grid(202, "M1")]           # someone opened M1 for editing
    f.sheet("TEMPLATE")["properties"]["hidden"] = False
    pr = f.sheet("README")["protectedRanges"][0]["protectedRangeId"]
    f.batch_update([{"deleteProtectedRange": {"protectedRangeId": pr}}])
    probs = sheets_setup.check(spec, f.meta(), "flagbook", rep["editors"])
    assert any("README: expected one whole-tab protection, found 0" in p for p in probs)
    assert any("extra editors ['intruder@example.org']" in p for p in probs)
    assert any("TEMPLATE: unprotected ranges differ" in p for p in probs)
    assert not any("TEMPLATE: should be hidden" in p for p in probs)   # operators unhide it to edit it
    f.sheet("_meta")["properties"]["hidden"] = False
    assert any("_meta: should be hidden" in p for p in sheets_setup.check(spec, f.meta(), "flagbook", rep["editors"]))
    f.batch_update([{"deleteSheet": {"sheetId": f.sheet("_meta")["properties"]["sheetId"]}}])
    assert any("tab '_meta' is missing" in p for p in sheets_setup.check(spec, f.meta(), "flagbook", rep["editors"]))


# ---- config and init -------------------------------------------------------------------------------
def test_sheets_config_validation(tmp_path):
    with pytest.raises(config.ConfigError) as e:
        config.load(project(tmp_path, tracker_id=None, flags_folder_id=None, links={}), check_paths=False)
    msg = str(e.value)
    assert "sheets.tracker_id is required" in msg and "sheets.links.qa_guide is required" in msg
    assert "sheets.flags_folder_id is required" in msg
    with pytest.raises(config.ConfigError, match="must be different"):
        config.load(project(tmp_path, flagbook_id="T"), check_paths=False)
    with pytest.raises(config.ConfigError, match="list of email addresses"):
        config.load(project(tmp_path, editors=["nope"]), check_paths=False)
    p = project(tmp_path)
    os.chmod(tmp_path / "key.json", 0o644)
    with pytest.raises(config.ConfigError, match="chmod 600"):
        config.load(p, check_paths=True)
    assert config.load(p, check_paths=False).sheets.editors == ["koch.eric.w@gmail.com"]


def test_unset_editors_fall_back_to_the_default(tmp_path):
    data = yaml.safe_load(project(tmp_path).read_text())
    del data["sheets"]["editors"]
    (tmp_path / "p2.yaml").write_text(yaml.safe_dump(data))
    cfg = config.load(tmp_path / "p2.yaml", check_paths=False)
    assert sheets_setup.editors_for(cfg, SA) == [SA, *sheets.DEFAULT_EDITORS]


def test_init_sheets_step(tmp_path, cfg):
    fakes = {"T": FakeSheets(spreadsheet_id="T"), "F": FakeSheets(spreadsheet_id="F")}
    root = Root(cfg.root)
    lines, code = init.sheets_step(cfg, root, make_client=fakes.__getitem__, drive=FakeDrive("FOLDER"))
    assert code == 0, lines
    assert any("flags folder FOLDER ('flags'): writable" in ln for ln in lines)
    assert "Track Summary" in [s["properties"]["title"] for s in fakes["T"].meta()["sheets"]]
    assert "TEMPLATE" in [s["properties"]["title"] for s in fakes["F"].meta()["sheets"]]
    assert list((cfg.root / "reports").glob("sheets-*.json"))
    # a live controller blocks it
    write_json(root.lease, {"holder": "abc", "heartbeat": iso(), "released": False})
    lines, code = init.sheets_step(cfg, root, make_client=fakes.__getitem__, drive=FakeDrive("FOLDER"))
    assert code == 2 and "holds the lease" in lines[0]


def test_init_repairs_a_template_that_was_typed_into_and_unlocked(cfg):
    """2026-11-24, live: a reviewer typed a flag row and RESTART into TEMPLATE (then visible, with
    reviewer-editable cells). Rerunning the build hides it, locks it to operators and clears the
    verdict; flag rows are the project's defaults and stay."""
    f = FakeSheets()
    sheets_setup.build(cfg, f, "flagbook", SA)
    sh = f.sheet("TEMPLATE")
    sh["properties"]["hidden"] = False
    sh["protectedRanges"][0]["unprotectedRanges"] = [sheets._grid(202, a) for a in ("D7:O150", "M1", "O1")]
    for a1, v in (("M1", "RESTART"), ("D7", True), ("E7", "TEST"), ("F7", "IC10*"), ("H7", 2)):
        f.set_value("TEMPLATE", a1, v)
    rep = sheets_setup.build(cfg, f, "flagbook", SA)
    assert rep["problems"] == [] and len(rep["repaired"]) == 3, rep
    assert rep["repaired"][2].startswith("cleared M1 ") and "E7" not in rep["repaired"][2]
    assert f.sheet("TEMPLATE")["properties"]["hidden"] is True
    assert f.get_value("TEMPLATE", "M1") is None
    assert (f.get_value("TEMPLATE", "D7"), f.get_value("TEMPLATE", "E7")) == (True, "TEST")   # default row kept
    assert f.get_value("TEMPLATE", "R7").startswith("=IF(")         # formulas kept
    assert sheets_setup.build(cfg, f, "flagbook", SA)["repaired"] == []   # nothing left to repair
