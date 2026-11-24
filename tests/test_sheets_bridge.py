"""M4: the Sheets bridge against FakeSheets.

End to end, the real controller runs as a subprocess (fake backend, placeholder workers) with a
file-backed FakeSheets (TM_FAKE_SHEETS_DIR) that this test also edits as the reviewer would.
The in-process tests drive one Bridge directly for the cases that are hard to time from outside
(a reviewer editing M1 between read and clear, an outage, drift, lost bridge state)."""
import re
from pathlib import Path

import pytest
import yaml

from helpers import make_project
from test_fake_backend import SKEL, controller, read_json, read_jsonl
from trackmanager import config as projconfig
from trackmanager import gsheets, sheets, sheets_setup
from trackmanager.controller import Controller
from trackmanager.fake_sheets import FakeSheets, FileFakeSheets
from trackmanager.sheets_bridge import FAKE_SA, Bridge
from trackmanager.store import create_json, write_json

SHEETS = {"enabled": True, "tracker_id": "T", "flagbook_id": "F", "credentials": "secrets/none.json",
          "editors": ["op@example.org"], "protection_check_cycles": 1, "drop_done_tabs_after_d": 0,
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
    """The reviewer's (and the test's) view of the two fake spreadsheets."""

    def __init__(self, fake, cfg_path):
        self.fake = fake
        cfg = projconfig.load(cfg_path, check_paths=False)
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
        return [s["properties"] for s in self.f().meta()["sheets"]
                if s["properties"]["title"] not in ("README", "TEMPLATE", "_meta")]

    def cell(self, title, a1):
        return self.f().get_value(title, a1)

    def put(self, title, a1, value):
        self.f().set_value(title, a1, value)

    def flag_row(self, title, row, **cols):
        f = self.f()
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
    assert sh.tracker("QA Sign-up", 2, "Flags URL").endswith(f"#gid={tabs[0]['sheetId']}")
    assert [p for p in sh.f().sheet(title)["protectedRanges"]]          # the copy is protected

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
    assert {i["item"] for i in summ["sheets"]["needs_you"]} >= {"verdict_has_problems"}
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
    assert [t.get("hidden") for t in sh.flag_tabs()] == [True]          # exported and hidden at DONE
    assert sh.cell(title, "O2") == "COMPLETED"
    evs = [e["reason"] for e in read_jsonl(root / "events.jsonl")]
    assert any("flag tab 'D_trk-a' created" in e for e in evs)
    assert any("exported to tracks/trk-a/flag_tab.csv and hidden" in e for e in evs)
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
    ctl = Controller(cfg, root, backend=None, me="t")
    states = {}
    for eb, stage in ebs_stages.items():
        st = {"eb": eb, "stage": stage, "iteration": 1, "attempt": 1, "hold": False, "spec": {"config": "C"},
              "activity": "running" if stage == "CALIBRATING" else None, "updated": "2026-11-24T00:00:00Z"}
        write_json(ctl.r.state(eb), st)
        states[eb] = st
    b = Bridge(ctl, tracker=t, flagbook=f)
    return b, ctl, states, t, f


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


def test_done_tab_is_exported_hidden_then_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "AWAITING_REVIEW"})
    b.publish(states)
    title = b.state["tabs"]["x"]["title"]
    f.set_value(title, "E7", "a flag row")
    states["x"]["stage"] = "DONE"
    b.ingest()
    b.publish(states)
    assert f.sheet(title)["properties"]["hidden"] is True
    assert "a flag row" in (ctl.r.track("x") / "flag_tab.csv").read_text()
    assert f.get_value(title, "O2") == "COMPLETED"
    b.publish(states)                                           # drop_done_tabs_after_d: 0
    assert title not in [s["properties"]["title"] for s in f.meta()["sheets"]]
    assert b.state["tabs"]["x"]["dropped_at"] and not any(k.startswith(f"F|{title}|") for k in b.mirror)
    assert t.get_value("Track Summary", f"{sheets.letters(sheets.tab(b.spec, 'Track Summary')['columns'])['Flags tab']}2") == ""


def test_m1_changed_between_read_and_clear_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "AWAITING_REVIEW"})
    b.publish(states)
    title = b.state["tabs"]["x"]["title"]
    f.set_value(title, "M1", "COMPLETE")
    b.ingest()
    p = b.state["pending"]["x"]
    d = read_json(ctl.r.inbox / p["file"])
    assert d["action"] == "pass" and d["verdict_word"] == "COMPLETE"
    # the controller processed it ... and meanwhile the reviewer typed a new verdict
    ctl.r.inbox_archive.mkdir(parents=True)
    write_json(ctl.r.inbox_archive / p["file"], {**d, "result": "applied"})
    (ctl.r.inbox / p["file"]).unlink()
    f.set_value(title, "M1", "HELP REQUESTED")
    b.publish(states)
    assert f.get_value(title, "M1") == "HELP REQUESTED"
    assert f.get_value(title, "M2").startswith("iteration 1: COMPLETE applied")


def test_outage_changes_nothing_and_alarms_after_three(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "AWAITING_REVIEW"})
    b.publish(states)
    f.set_value(b.state["tabs"]["x"]["title"], "M1", "COMPLETE")

    def down(*a, **k):
        raise gsheets.SheetsError("HTTP 503: backend error", 503)
    monkeypatch.setattr(f, "values_get", down)
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
    sh = f.sheet(b.state["tabs"]["x"]["title"])
    f.batch_update([{"deleteProtectedRange": {"protectedRangeId": sh["protectedRanges"][0]["protectedRangeId"]}}])
    b.publish(states)
    assert "protection drift" in b.alarm() and "C_x" in b.alarm()
    assert any("protection drift" in e["reason"] for e in read_jsonl(ctl.r.events))


def test_lost_bridge_state_finds_existing_rows_and_tabs(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"a": "AWAITING_REVIEW", "b": "CALIBRATING"})
    b.publish(states)
    (ctl.r.path / "sheets" / "bridge.json").unlink()
    (ctl.r.path / "sheets" / "mirror.json").unlink()
    b2 = Bridge(ctl, tracker=t, flagbook=f)
    b2.publish(states)
    assert b2.state["rows"]["Track Summary"] == {"a": 2, "b": 3}
    assert sorted(b2.state["tabs"]) == ["a", "b"] and all(v.get("adopted") for v in b2.state["tabs"].values())
    assert len(f.meta()["sheets"]) == 5                       # README, TEMPLATE, _meta + 2, no duplicates
    assert t.get_value("Track Summary", "C4") is None


def test_formula_like_text_is_written_as_text(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_FAKE_SHEETS_DIR", str(tmp_path))
    b, ctl, states, t, f = bridge_for(tmp_path, {"x": "DISCOVERED"})
    states["x"]["detail"] = "=IMPORTXML(\"http://evil\")"
    b.publish(states)
    lm = sheets.letters(sheets.tab(b.spec, "Track Summary")["columns"])
    assert t.get_value("Track Summary", f"{lm['Detail']}2") == "'=IMPORTXML(\"http://evil\")"
