"""M4: the Sheets bridge against FakeSheets / FakeDrive.

End to end, the real controller runs as a subprocess (fake backend, placeholder workers) with
file-backed fakes (TM_FAKE_SHEETS_DIR): the tracker, the template workbook and one spreadsheet per
track in the flags folder, which this test also edits as the reviewer would. The in-process tests
drive one Bridge directly for the cases that are hard to time from outside (a reviewer editing M1
between read and clear, an outage, drift, lost bridge state)."""
import re
from pathlib import Path

import pytest
import yaml

from helpers import make_fake_sdm, make_project
from test_fake_backend import SKEL, controller, read_json, read_jsonl
from test_stages import decide
from trackmanager import config as projconfig
from trackmanager import gsheets, sheets, sheets_setup
from trackmanager.controller import Controller
from trackmanager.fake_sheets import FakeDrive, FakeSheets, FileFakeSheets
from trackmanager.sheets_bridge import FAKE_SA, Bridge
from trackmanager.store import create_json, write_json

SHEETS = {"enabled": True, "tracker_id": "T", "flagbook_id": "F", "flags_folder_id": "FOLDER",
          "credentials": "secrets/none.json", "editors": ["op@example.org"], "protection_check_cycles": 1,
          "links": {"qa_guide": "https://example.org/g", "flag_rows_help": "https://example.org/h"}}


def setup(tmp_path, tracks, **sheets_over):
    path, root = make_project(tmp_path, tracks, review=True, sheets={**SHEETS, **sheets_over})
    cfg = projconfig.load(path, check_paths=False)
    fake = tmp_path / "fakesheets"
    fake.mkdir()
    for which, sid in (("tracker", "T"), ("flagbook", "F")):
        sheets_setup.build(cfg, FileFakeSheets(fake / f"{sid}.pkl", spreadsheet_id=sid), which, FAKE_SA)
    return path, root, fake


class Sheet:
    """The reviewer's (and the test's) view of the fake spreadsheets: the tracker, the template
    workbook ("TEMPLATE") and each track's own flag spreadsheet (by its tab title)."""

    def __init__(self, fake, cfg_path):
        self.fake = fake
        cfg = projconfig.load(cfg_path, check_paths=False)
        self.root = cfg.root
        self.spec = sheets.load_spec(sheets.SPEC, sheets_setup.project_values(cfg))

    def t(self):
        return FileFakeSheets(self.fake / "T.pkl", spreadsheet_id="T")

    def f(self):
        return FileFakeSheets(self.fake / "F.pkl", spreadsheet_id="F")

    def col(self, tab, name):
        return sheets.letters(sheets.tab(self.spec, tab)["columns"])[name]

    def tracker(self, tab, row, name):
        return self.t().get_value(tab, f"{self.col(tab, name)}{row}")

    def sign_up(self, row, name, email):
        t = self.t()
        t.set_value("QA Sign-up", f"{self.col('QA Sign-up', 'QA Reviewer')}{row}", name)
        t.set_value("QA Sign-up", f"{self.col('QA Sign-up', 'Email')}{row}", email)

    def flag_tabs(self):
        """The bridge's record of each track's flag sheet: title, spreadsheet_id, gid."""
        return list((read_json(self.root / "sheets" / "bridge.json") or {}).get("tabs", {}).values())

    def book(self, title):
        if title == "TEMPLATE":
            return self.f()
        sid = next(t["spreadsheet_id"] for t in self.flag_tabs() if t["title"] == title)
        return FileFakeSheets(self.fake / f"{sid}.pkl", spreadsheet_id=sid)

    def cell(self, title, a1):
        return self.book(title).get_value(title, a1)

    def put(self, title, a1, value):
        self.book(title).set_value(title, a1, value)

    def flag_row(self, title, row, **cols):
        f = self.book(title)
        names = {c["name"]: c["col"] for c in self.spec["flagbook"]["table"]["columns"]}
        for k, v in cols.items():
            f.set_value(title, f"{names[k.replace('_', ' ')]}{row}", v)


def run(cfg, root, fake, cycles):
    p = controller(cfg, root, "--max-cycles", str(cycles), env={"TM_FAKE_SHEETS_DIR": str(fake)}, timeout=180)
    assert p.returncode == 0, p.output
    return p


def state(root, eb):
    return read_json(root / "state" / f"{eb}.json")


def until(cfg, root, fake, eb, pred, tries=8):
    for _ in range(tries):
        run(cfg, root, fake, 3)
        if pred(state(root, eb)):
            return state(root, eb)
    raise AssertionError(f"{eb} never reached the expected state: {state(root, eb)}")


def test_review_loop_through_the_sheets(tmp_path):
    cfg, root, fake = setup(tmp_path, [{"eb": "trk-a", "mode": "ok", "sleep_s": 1, "config": "D"}])
    sh = Sheet(fake, cfg)
    st = until(cfg, root, fake, "trk-a", lambda s: s["stage"] == "AWAITING_REVIEW")

    # rows and the flag tab
    assert sh.tracker("Track Summary", 2, "Trackname") == "trk-a"
    assert sh.tracker("Track Summary", 2, "Stage") == "AWAITING_REVIEW"
    assert sh.tracker("QA Sign-up", 2, "Stage") == "AWAITING_REVIEW"
    tabs = sh.flag_tabs()
    assert [t["title"] for t in tabs] == ["D_trk-a"]
    title = "D_trk-a"
    assert sh.cell(title, "H1") == 1 and sh.cell(title, "E1") == "trk-a"
    url = sh.tracker("QA Sign-up", 2, "Flags URL")
    assert url == f"https://docs.google.com/spreadsheets/d/{tabs[0]['spreadsheet_id']}/edit#gid={tabs[0]['gid']}"
    book = sh.book(title)                                               # its own spreadsheet, in the folder
    assert [s["properties"]["title"] for s in book.meta()["sheets"]] == [title, "README"]
    assert len(book.sheet(title)["protectedRanges"][0]["unprotectedRanges"]) == 3   # reviewer ranges open
    files = FakeDrive("FOLDER", directory=fake).files
    assert [(f["name"], f["parents"], f["appProperties"]["trackmanager_eb"]) for f in files.values()] == \
        [("t flags D_trk-a", ["FOLDER"], "trk-a")]

    # the script's column is never written by the bridge
    sh.t().set_value("QA Sign-up", f"{sh.col('QA Sign-up', 'Notified iter')}2", 1)
    sh.sign_up(2, "Reviewer A", "a@example.org")
    # a ticked row with a bad time: the verdict stays, the problem shows
    sh.flag_row(title, 7, Apply_Row=True, Issue_Description="ea10's bad", Scan="5", Antenna_Name="ea10",
                **{"Start_Date/Time": "2022/07/26 09:00"}, Name_of_QA_person="Reviewer A")
    sh.put(title, "M1", "RESTART")
    run(cfg, root, fake, 2)
    assert state(root, "trk-a")["iteration"] == 1
    assert sh.cell(title, "M1") == "RESTART"
    assert "no decimals" in sh.cell(title, "T7") and "RESTART not applied yet" in sh.cell(title, "M2")
    summ = read_json(root / "state" / "summary.json")
    assert {i["item"] for i in summ["sheets"]["needs_you"]} >= {"flag_rows_have_problems"}
    assert sh.tracker("Track Summary", 2, "Reviewer") == "Reviewer A"
    assert sh.tracker("Track Summary", 2, "Signed up (UTC)")

    # fix it, add a second row and an unticked one, avoid ea26 as refant
    sh.flag_row(title, 7, **{"Start_Date/Time": ""})
    sh.flag_row(title, 8, Apply_Row=True, Issue_Description="RFI", Field_Name="3C48", SPW="7",
                Name_of_QA_person="Reviewer A")
    sh.flag_row(title, 9, Apply_Row=False, Issue_Description="not this", Scan="3", Name_of_QA_person="A")
    sh.put(title, "O1", "ea26")
    st = until(cfg, root, fake, "trk-a", lambda s: s["iteration"] == 2 and s["stage"] == "AWAITING_REVIEW")
    flags = (root / "tracks" / "trk-a" / "iter_2" / "inputs" / "manual_flagging.txt").read_text().splitlines()
    assert flags[2:] == ["mode='manual' scan='5' antenna='ea10' reason='[MANUAL] Issue: ea10s bad. Flagged by Reviewer A'",
                         "mode='manual' field='3C48' spw='7' reason='[MANUAL] Issue: RFI. Flagged by Reviewer A'"]
    assert (root / "tracks" / "trk-a" / "iter_2" / "inputs" / "refant_ignore.txt").read_text() == "ea26\n"
    assert sh.cell(title, "M1") in ("", None) and sh.cell(title, "M2").startswith("iteration 1: RESTART applied")
    assert (sh.cell(title, "S7"), sh.cell(title, "S8"), sh.cell(title, "S9")) == (2, 2, None)
    assert sh.cell(title, "T7") == "" and sh.cell(title, "H1") == 2
    rv = read_json(root / "tracks" / "trk-a" / "iter_1" / "review.json")
    assert (rv["verdict"], rv["verdict_word"], rv["reviewer"], rv["source"]) == ("restart", "RESTART", "Reviewer A", "sheets")
    assert rv["signed_up"] and rv["by"] == "Reviewer A"
    assert sh.tracker("QA Sign-up", 2, "Notified iter") == 1                  # untouched
    assert sh.tracker("QA Sign-up", 2, "QA Reviewer") == "Reviewer A"
    assert sh.tracker("Track Summary", 2, "Verdict") == "RESTART (iteration 1)"
    assert sh.tracker("Track Summary", 2, "Avoid as refant") == "ea26"

    # untick row 8 (cumulative flags: unticking drops it), then hold for the operator
    sh.flag_row(title, 8, Apply_Row=False)
    sh.put(title, "M1", "MANUAL REVIEW")
    run(cfg, root, fake, 2)
    st = state(root, "trk-a")
    assert st["hold"] and st["hold_reason"] == "manual_review" and st["stage"] == "AWAITING_REVIEW"
    assert sh.tracker("Track Summary", 2, "Detail").startswith("HOLD: manual review")
    assert "reviewer_hold" in {i["item"] for i in read_json(root / "state" / "summary.json")["sheets"]["needs_you"]}

    # COMPLETE clears the hold and finishes the track; the controller then exits by itself
    sh.put(title, "M1", "COMPLETE")
    p = controller(cfg, root, "--max-cycles", "10", env={"TM_FAKE_SHEETS_DIR": str(fake)}, timeout=180)
    assert p.returncode == 0, p.output
    st = state(root, "trk-a")
    assert st["stage"] == "DONE" and not st["hold"] and st["completed_by"] == "Reviewer A"
    m = re.search(r"stopping after cycle (\d+): stages \['DONE'\]", p.output)
    assert m and int(m.group(1)) < 10, p.output[-2000:]                # exits once every track is terminal
    assert sh.tracker("Track Summary", 2, "Stage") == "DONE"
    assert sh.tracker("Track Summary", 2, "Verdict") == "COMPLETE (iteration 2)"
    assert (root / "tracks" / "trk-a" / "flag_tab.csv").read_text().count("ea10") >= 1
    assert not sh.book(title).sheet(title)["properties"].get("hidden")   # the sheet stays, visible
    assert sh.cell(title, "O2") == "COMPLETED"
    evs = [e["reason"] for e in read_jsonl(root / "events.jsonl")]
    assert any("flag sheet 't flags D_trk-a' created" in e for e in evs)
    assert any("saved to tracks/trk-a/flag_tab.csv (the sheet stays" in e for e in evs)
    assert any(e.startswith("QA sign-up: Reviewer A") for e in evs)


def test_verdict_before_review_is_rejected_and_cleared(tmp_path):
    cfg, root, fake = setup(tmp_path, [{"eb": "slow", "mode": "ok", "sleep_s": 25}])
    sh = Sheet(fake, cfg)
    until(cfg, root, fake, "slow", lambda s: s["stage"] == "CALIBRATING" and s.get("activity") == "running", tries=4)
    run(cfg, root, fake, 1)
    title = "slow"
    assert [t["title"] for t in sh.flag_tabs()] == [title]                 # created when calibration started
    sh.put(title, "M1", "COMPLETE")
    run(cfg, root, fake, 1)
    assert sh.cell(title, "M1") in ("", None) and "not waiting for review" in sh.cell(title, "M2")
    st = until(cfg, root, fake, "slow", lambda s: s["stage"] == "AWAITING_REVIEW")
    assert st["stage"] == "AWAITING_REVIEW"                               # the early COMPLETE did not pass it


# ---- in-process --------------------------------------------------------------------------------
def bridge_for(tmp_path, ebs_stages, **sheets_over):
    path, root = make_project(tmp_path, [], review=True, sheets={**SHEETS, **sheets_over})
    cfg = projconfig.load(path, check_paths=False)
    t, f = FakeSheets(spreadsheet_id="T"), FakeSheets(spreadsheet_id="F")
    sheets_setup.build(cfg, t, "tracker", FAKE_SA)
    sheets_setup.build(cfg, f, "flagbook", FAKE_SA)
    drive = FakeDrive("FOLDER")
    ctl = Controller(cfg, root, backend=None, me="t")
    states = {}
    for eb, stage in ebs_stages.items():
        st = {"eb": eb, "stage": stage, "iteration": 1, "attempt": 1, "hold": False, "spec": {"config": "C"},
              "activity": "running" if stage == "CALIBRATING" else None, "updated": "2026-11-24T00:00:00Z"}
        write_json(ctl.r.state(eb), st)
        states[eb] = st
    b = Bridge(ctl, tracker=t, flagbook=f, drive=drive, books=FakeSheets.REGISTRY.__getitem__)
    return b, ctl, states, t, f


def tab_of(b, eb):
    """(track's spreadsheet, its flag tab title)."""
    return b.book(eb), b.state["tabs"][eb]["title"]


def test_tabs_only_from_calibrating_and_rows_for_every_track(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))    # fake service account
    b, ctl, states, t, f = bridge_for(tmp_path, {"a": "DISCOVERED", "b": "STAGING", "c": "CALIBRATING",
                                                 "d": "AWAITING_REVIEW", "e": "SKIPPED"})
    b.publish(states)
    assert sorted(b.state["tabs"]) == ["c", "d"]
    assert [t.get_value("Track Summary", f"C{r}") for r in range(2, 7)] == ["a", "b", "c", "d", "e"]
    assert b.status()["alarm"] is None and b.status()["drift"] == []
    calls = t.calls
    b.publish(states)                                           # nothing changed: no tracker write
    assert t.calls == calls + 1                                 # only the protection check's metadata read


def test_done_sheet_is_saved_and_stays(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "AWAITING_REVIEW"})
    b.publish(states)
    book, title = tab_of(b, "x")
    book.set_value(title, "E7", "a flag row")
    states["x"]["stage"] = "DONE"
    b.ingest()
    b.publish(states)
    assert "a flag row" in (ctl.r.track("x") / "flag_tab.csv").read_text()
    assert book.get_value(title, "O2") == "COMPLETED" and not book.sheet(title)["properties"].get("hidden")
    calls = book.calls
    b.publish(states)                                           # saved once; never hidden or deleted
    assert book.calls == calls
    lm = sheets.letters(sheets.tab(b.spec, "Track Summary")["columns"])
    assert t.get_value("Track Summary", f"{lm['Flags tab']}2").startswith('=HYPERLINK("https://docs.google.com/')


def test_m1_changed_between_read_and_clear_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "AWAITING_REVIEW"})
    b.publish(states)
    book, title = tab_of(b, "x")
    book.set_value(title, "M1", "COMPLETE")
    b.ingest()
    p = b.state["pending"]["x"]
    d = read_json(ctl.r.inbox / p["file"])
    assert d["action"] == "pass" and d["verdict_word"] == "COMPLETE"
    # the controller processed it ... and meanwhile the reviewer typed a new verdict
    ctl.r.inbox_archive.mkdir(parents=True)
    write_json(ctl.r.inbox_archive / p["file"], {**d, "result": "applied"})
    (ctl.r.inbox / p["file"]).unlink()
    book.set_value(title, "M1", "HELP REQUESTED")
    b.publish(states)
    assert book.get_value(title, "M1") == "HELP REQUESTED"
    assert book.get_value(title, "M2").startswith("iteration 1: COMPLETE applied")


def test_outage_changes_nothing_and_alarms_after_three(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "AWAITING_REVIEW"})
    b.publish(states)
    book, title = tab_of(b, "x")
    book.set_value(title, "M1", "COMPLETE")

    def down(*a, **k):
        raise gsheets.SheetsError("HTTP 503: backend error", 503)
    monkeypatch.setattr(book, "values_get", down)
    for _ in range(3):
        b.ingest()
    assert not ctl.r.inbox.exists() or not list(ctl.r.inbox.glob("*.json"))
    assert b.alarm().startswith("read: HTTP 503") and b.health["consecutive_failures"] == 3
    monkeypatch.undo()
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b.ingest()
    b.publish(states)
    assert b.alarm() is None and b.state["pending"].get("x")


def test_protection_drift_on_a_flag_tab_raises_the_alarm(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "CALIBRATING"})
    b.publish(states)
    book, title = tab_of(b, "x")
    sh = book.sheet(title)
    book.batch_update([{"deleteProtectedRange": {"protectedRangeId": sh["protectedRanges"][0]["protectedRangeId"]}}])
    b.publish(states)
    assert "protection drift" in b.alarm() and "flags C_x" in b.alarm()
    assert any("protection drift" in e["reason"] for e in read_jsonl(ctl.r.events))


def test_lost_bridge_state_finds_existing_rows_and_tabs(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"a": "AWAITING_REVIEW", "b": "CALIBRATING"})
    b.publish(states)
    (ctl.r.path / "sheets" / "bridge.json").unlink()
    (ctl.r.path / "sheets" / "mirror.json").unlink()
    b2 = Bridge(ctl, tracker=t, flagbook=f, drive=b.drive, books=FakeSheets.REGISTRY.__getitem__)
    b2.publish(states)
    assert b2.state["rows"]["Track Summary"] == {"a": 2, "b": 3}
    assert sorted(b2.state["tabs"]) == ["a", "b"] and all(v.get("adopted") for v in b2.state["tabs"].values())
    assert len(b.drive.files) == 2                            # found by appProperties, not made twice
    assert b2.state["tabs"]["a"]["gid"] == b.state["tabs"]["a"]["gid"]
    assert t.get_value("Track Summary", "C4") is None


def test_formula_like_text_is_written_as_text(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "DISCOVERED"})
    states["x"]["detail"] = "=IMPORTXML(\"http://evil\")"
    b.publish(states)
    lm = sheets.letters(sheets.tab(b.spec, "Track Summary")["columns"])
    assert t.get_value("Track Summary", f"{lm['Detail']}2") == "'=IMPORTXML(\"http://evil\")"


def test_template_rows_are_copied_but_its_verdict_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "STAGING"})
    for a1, v in (("M1", "RESTART"), ("D7", True), ("E7", "TEST"), ("S7", 2)):
        f.set_value("TEMPLATE", a1, v)
    b.publish(states)
    assert "TEMPLATE: has input in M1, S7 " in b.alarm()           # rows are fine; the verdict is not
    states["x"]["stage"] = "CALIBRATING"
    b.publish(states)
    book, title = tab_of(b, "x")
    assert [book.get_value(title, a) for a in ("M1", "D7", "E7", "S7")] == [None, True, "TEST", None]
    assert book.get_value(title, "R7").startswith("=IF(") and book.sheet(title)["properties"].get("hidden") is not True
    prot, = book.sheet(title)["protectedRanges"]                    # reviewer protection, not TEMPLATE's lock
    assert len(prot["unprotectedRanges"]) == 3
    b.ingest()
    assert not b.state["pending"] and "x" not in b.state["acks"]    # no verdict in the copy


def test_iteration_1_runs_with_the_ticked_rows_of_its_tab(tmp_path):
    """The project TEMPLATE's default rows are copied into each new track tab; iteration 1 waits a
    cycle for the tab, then freezes its ticked rows (a row that fails the checks is left out)."""
    cfg, root, fake = setup(tmp_path, [{"eb": "t1", "mode": "ok", "sleep_s": 1, "config": "B"}])
    sh = Sheet(fake, cfg)
    sh.flag_row("TEMPLATE", 7, Apply_Row=True, Issue_Description="known RFI", SPW="7", Name_of_QA_person="ops")
    sh.flag_row("TEMPLATE", 8, Apply_Row=False, Issue_Description="sometimes", Scan="2", Name_of_QA_person="ops")
    sh.flag_row("TEMPLATE", 9, Apply_Row=True, Issue_Description="bad default", Name_of_QA_person="ops",
                **{"Start_Date/Time": "yesterday"})
    sh.put("TEMPLATE", "O1", "ea05")                       # never copied
    until(cfg, root, fake, "t1", lambda s: s["stage"] == "AWAITING_REVIEW")
    flags = (root / "tracks" / "t1" / "iter_1" / "inputs" / "manual_flagging.txt").read_text().splitlines()
    assert flags[2:] == ["mode='manual' spw='7' reason='[MANUAL] Issue: known RFI. Flagged by ops'"]
    assert not (root / "tracks" / "t1" / "iter_1" / "inputs" / "refant_ignore.txt").exists()
    title = "B_t1"
    assert (sh.cell(title, "S7"), sh.cell(title, "S8"), sh.cell(title, "S9")) == (1, None, None)
    assert "not used in iteration 1" in sh.cell(title, "T9") and sh.cell(title, "O1") is None
    assert sh.cell(title, "M2").startswith("iteration 1 started") and "1 ticked row(s) left out" in sh.cell(title, "M2")
    evs = read_jsonl(root / "events.jsonl")
    assert any("iteration 1 started with 1 ticked flag row(s)" in e["reason"] for e in evs)
    waits = [r for r in read_jsonl(root / "jobs" / "jobs.jsonl") if r.get("eb") == "t1" and r.get("status") == "submitted"]
    assert len(waits) == 1                                  # one submission, after the one-cycle wait



def test_reopen_after_a_mistaken_complete_restages_and_uses_the_sheet(tmp_path):
    """2026-11-24: a COMPLETE set by mistake. The sheet stays editable after DONE; the operator
    reopens the track; the raw data (deleted at DONE) is staged again; the next run uses the
    sheet's ticked rows, edits made after DONE included; the promoted splits are kept aside."""
    (tmp_path / "in").mkdir()
    sdm = make_fake_sdm(tmp_path / "in")
    cfg, root, fake = setup(tmp_path, [{"eb": "r", "path": str(sdm), "mode": "ok", "sleep_s": 1, "config": "D"}])
    sh = Sheet(fake, cfg)
    until(cfg, root, fake, "r", lambda s: s["stage"] == "AWAITING_REVIEW")
    title, = [t["title"] for t in sh.flag_tabs()]
    assert title == f"D_{sdm.name}"                         # a staged track's sheet is named after its SDM
    sh.flag_row(title, 7, Apply_Row=True, Issue_Description="RFI", SPW="7", Name_of_QA_person="R")
    sh.put(title, "M1", "COMPLETE")                         # meant RESTART
    until(cfg, root, fake, "r", lambda s: s["stage"] == "DONE")
    assert not (root / "tracks" / "r" / "raw").exists() and (root / "science" / "r").is_dir()
    decide(root, "r", "reopen")
    sh.flag_row(title, 8, Apply_Row=True, Issue_Description="more RFI", Scan="3", Name_of_QA_person="R")
    st = until(cfg, root, fake, "r", lambda s: s["iteration"] == 2 and s["stage"] == "AWAITING_REVIEW")
    flags = (root / "tracks" / "r" / "iter_2" / "inputs" / "manual_flagging.txt").read_text().splitlines()
    assert flags[2:] == ["mode='manual' spw='7' reason='[MANUAL] Issue: RFI. Flagged by R'",
                         "mode='manual' scan='3' reason='[MANUAL] Issue: more RFI. Flagged by R'"]
    rec, = st["reopened"]
    assert rec["from_iteration"] == 1 and rec["completed_iteration"] == 1 and "completed_at" not in st
    assert rec["superseded"]["science"].startswith(str(root / "science" / "r.superseded-"))
    assert Path(rec["superseded"]["science"]).is_dir() and not (root / "science" / "r").exists()
    assert rec["manifest_before"] and Path(rec["manifest_before"]).exists()
    assert read_json(root / "tracks" / "r" / "stage" / "manifest.json")["ok"] and st["stage_attempt"] == 2
    moves = {(e["from"], e["to"]) for e in read_jsonl(root / "events.jsonl")}
    assert ("DONE", "STAGING") in moves
    assert sh.cell(title, "O2") in ("", None) and sh.cell(title, "H1") == 2
    assert sh.tracker("Track Summary", 2, "Verdict") == "COMPLETE (iteration 1, reopened)"
    assert (sh.cell(title, "S7"), sh.cell(title, "S8")) == (2, 2)
    assert len(sh.flag_tabs()) == 1                         # the same sheet, not a new one


def test_reopen_without_an_input_goes_back_to_calibrating_and_only_from_done(tmp_path):
    cfg, root, fake = setup(tmp_path, [{"eb": "p", "mode": "ok", "sleep_s": 1}])
    sh = Sheet(fake, cfg)
    until(cfg, root, fake, "p", lambda s: s["stage"] == "AWAITING_REVIEW")
    decide(root, "p", "reopen")                             # not DONE: ignored
    run(cfg, root, fake, 1)
    assert state(root, "p")["stage"] == "AWAITING_REVIEW"
    assert any("reopen applies to DONE tracks" in f.read_text() for f in (root / "inbox" / "archive").glob("*.json"))
    sh.put("p", "M1", "COMPLETE")
    until(cfg, root, fake, "p", lambda s: s["stage"] == "DONE")
    decide(root, "p", "reopen", "--notes", "completed by mistake")
    st = until(cfg, root, fake, "p", lambda s: s["iteration"] == 2 and s["stage"] == "AWAITING_REVIEW")
    assert ("DONE", "CALIBRATING") in {(e["from"], e["to"]) for e in read_jsonl(root / "events.jsonl")}
    assert st["reopened"][0]["notes"] == "completed by mistake"
