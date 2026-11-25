"""Sheets bridge (plan §3.5, M4): a synchronous step of the controller's cycle that mirrors the
track database to the tracker and to one flag spreadsheet per track, and reads reviewers'
sign-ups, flag rows and verdicts back.

Google side (decided 2026-11-24): the tracker (Track Summary, QA Sign-up); a template workbook
(`sheets.flagbook_id`: README and the project's flag TEMPLATE, which holds its likely flags); and a
Drive folder (`sheets.flags_folder_id`) where the service account creates one spreadsheet per track
when it is first queued for calibration, copied from TEMPLATE. Track spreadsheets are never hidden
or deleted: they stay readable, and editable, after DONE, so a track completed by mistake can be
reopened (operator action `reopen`) and its next run uses the sheet's ticked rows.

    bridge.ingest()        before inbox.apply: read the sign-up columns and the flag sheet of every
                           track that is calibrating or in review; stamp sign-ups; turn a valid verdict
                           (M1) into inbox/decisions/sheets-*.json (RESTART carries the flag file rebuilt
                           from ALL ticked rows by flagcmds, plus O1 as refant_ignore.txt); a verdict
                           with problems stays in M1, with the problems in the row's Check cell and M2
    bridge.run_inputs(st)  from calibrate.submit, before the first attempt of an iteration whose
                           inputs are not frozen yet (iteration 1, or the run after a reopen): freeze
                           the ticked rows of the track's sheet into iter_<n>/inputs/
    bridge.publish(states) after the summaries: acknowledge processed verdicts (M2, clear M1, stamp
                           "Added in iter"), create track spreadsheets, save a finished track's sheet
                           to tracks/<EB>/flag_tab.csv, write every changed controller cell (one values
                           write per spreadsheet that changed), check protections every N cycles

The controller stays the only writer of state/: the bridge only writes inbox/, iteration inputs it
is asked to freeze, and its own <root>/sheets/ files:
  bridge.json   spreadsheet ids, row of each track per tracker tab, each track's flag spreadsheet,
                sign-ups, acks, per-row problems, verdicts waiting for the controller
  mirror.json   the last value written to each controller cell (writes are diffs against it)
  status.json   health (last ok, failures in a row, error), protection drift, "needs you" items
A Google failure changes no track state: the step is skipped, counted, and from 3 failures in a
row raises the alarm (heartbeat, state/summary.json, trackmanager.status).
Reviewer cells (QA Reviewer, Email, Notes, M1, O1, the flag rows) and the script's Notified iter
are never in a write range, except that M1 is cleared once its verdict has been acknowledged,
and only if it still holds the value that was read.
"""
from __future__ import annotations

import csv
import os
import re
import traceback
from pathlib import Path

from . import flagcmds, gsheets, sheets, sheets_setup
from . import states as S
from .sheet_context import archive_ctx, track_context
from .store import age_s, create_json, iso, log, read_json, write_json

VERDICTS = {"COMPLETE": "pass", "RESTART": "restart", "MANUAL REVIEW": "manual_review",
            "HELP REQUESTED": "help_requested"}
BOOK_STAGES = (S.CALIBRATING, S.AWAITING_REVIEW, S.PROMOTING)   # a track gets its flag sheet here
READ_STAGES = (S.CALIBRATING, S.AWAITING_REVIEW)                # ... which is read while in these
DONE_STAGES = (S.DONE, S.SKIPPED, S.NEEDS_MANUAL)               # ... and saved to CSV after these
ALARM_AFTER = 3
REFANT_RE = re.compile(r"^ea\d\d(,ea\d\d)*$")
FAKE_SA = "fake-controller@example.iam.gserviceaccount.com"


def now_sheet() -> str:
    return iso().replace("T", " ").replace("Z", "")


def _cell(grid, row: int, col: str) -> str:
    """Value at 1-based row, column letter, from a values_get grid ('' when absent)."""
    r, c = row - 1, sheets.col_index(col)
    if r < len(grid) and c < len(grid[r]):
        v = grid[r][c]
        return "" if v is None else str(v)
    return ""


def _a1(tab, col, row):
    return f"{gsheets.a1(tab)}!{col}{row}"


class Bridge:
    def __init__(self, ctl, tracker=None, flagbook=None, drive=None, books=None):
        """tracker / flagbook (template workbook) / drive (flags folder) clients and books
        (spreadsheet id -> client) are made from the config unless given (tests)."""
        self.ctl, self.cfg, self.r = ctl, ctl.cfg, ctl.r
        self.dir = ctl.r.path / "sheets"
        self.tracker, self.flagbook, self.drive, self._mk = tracker, flagbook, drive, books
        self.books: dict = {}
        self.spec = sheets.load_spec(sheets.SPEC, sheets_setup.project_values(self.cfg))
        self.fb = self.spec["flagbook"]
        self.tb = self.fb["table"]
        self.sa = None
        self.started = False
        self.cycles = 0
        self.read = None                 # this cycle's ingest read ({"signup": grid, "tabs": {eb: grid}})
        self.clear_m1: dict = {}         # eb -> M1 value to clear (if still there)
        self.state = self._load()
        self.mirror = read_json(self.dir / "mirror.json") or {}
        self.health = (read_json(self.dir / "status.json") or {}).get("health") or {"consecutive_failures": 0}
        self.drift: list = (read_json(self.dir / "status.json") or {}).get("drift") or []

    # ---- own files ----------------------------------------------------------------------------
    def _ids(self):
        sh = self.cfg.sheets
        return {"tracker_id": sh.tracker_id, "flagbook_id": sh.flagbook_id, "flags_folder_id": sh.flags_folder_id}

    def _load(self):
        st = read_json(self.dir / "bridge.json") or {}
        self.mirror_reset = any(st.get(k) != v for k, v in self._ids().items())
        if self.mirror_reset:
            st = {}                      # other spreadsheets or folder: start over (rows, sheets, mirror)
        st.update(self._ids())
        for key in ("rows", "tabs", "signups", "acks", "checks", "pending", "stamps"):
            st.setdefault(key, {})
        for t in self.spec["tracker"]["tabs"]:
            st["rows"].setdefault(t["name"], {})
        return st

    def _save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json(self.dir / "bridge.json", self.state)
        write_json(self.dir / "mirror.json", self.mirror)

    def status(self) -> dict:
        return read_json(self.dir / "status.json") or {}

    # ---- clients and start-up ---------------------------------------------------------------------
    def _clients(self):
        fake = os.environ.get("TM_FAKE_SHEETS_DIR")
        sh = self.cfg.sheets
        if self._mk is None:
            if fake:                     # tests: file-backed fakes shared with the test process
                from .fake_sheets import FileFakeSheets
                self._mk = lambda sid: FileFakeSheets(Path(fake) / f"{sid}.pkl", spreadsheet_id=sid)
            else:
                self._mk = lambda sid: gsheets.SheetsClient(sid, credentials=self.cfg.sheets_key)
        if self.drive is None:
            if fake:
                from .fake_sheets import FakeDrive
                self.drive = FakeDrive(sh.flags_folder_id, directory=fake)
            else:
                self.drive = gsheets.DriveClient(sh.flags_folder_id, credentials=self.cfg.sheets_key)
        if self.tracker is None:
            self.tracker = self._mk(sh.tracker_id)
        if self.flagbook is None:
            self.flagbook = self._mk(sh.flagbook_id)

    def book(self, eb):
        """The client for a track's own flag spreadsheet."""
        sid = self.state["tabs"][eb]["spreadsheet_id"]
        if sid not in self.books:
            self.books[sid] = self._mk(sid)
        return self.books[sid]

    def _start(self):
        if self.started:
            return
        self._clients()
        try:
            self.sa = gsheets.service_account_email(self.cfg.sheets_key)
        except gsheets.KeyFileError:
            if not os.environ.get("TM_FAKE_SHEETS_DIR"):
                raise
            self.sa = FAKE_SA
        self.editors = sheets_setup.editors_for(self.cfg, self.sa)
        if self.mirror_reset:
            self.mirror = {}
        tm = self.tracker.meta("sheets(properties(sheetId,title))")
        have = {s["properties"]["title"] for s in tm["sheets"]}
        missing = [t["name"] for t in self.spec["tracker"]["tabs"] if t["name"] not in have]
        fm = self.flagbook.meta("sheets(properties(sheetId,title,hidden))")
        ftitles = {s["properties"]["title"]: s["properties"] for s in fm["sheets"]}
        for t in (self.fb["template_tab"], "README"):
            if t not in ftitles:
                missing.append(f"template workbook {t}")
        if missing:
            raise gsheets.SheetsError(f"spreadsheet tabs missing ({', '.join(missing)}): run "
                                      "python -m trackmanager.init --project <project.yaml> --sheets")
        # The flags folder is not read here: under drive.file it cannot be (init --sheets checks it
        # by writing). A folder that is not writable shows as a failed sheet creation and the alarm.
        self.template_gid = ftitles[self.fb["template_tab"]]["sheetId"]
        self.readme_gid = ftitles["README"]["sheetId"]
        if not any(self.state["rows"].values()):
            self._rebuild_rows()
        self.started = True
        log(f"sheets bridge started: service account {self.sa}; tracker {self.cfg.sheets.tracker_id}, "
            f"template workbook {self.cfg.sheets.flagbook_id}, flags folder {self.cfg.sheets.flags_folder_id}")

    def _key(self, eb) -> str:
        rec = read_json(self.r.archive_json(eb)) or {}
        st = read_json(self.r.state(eb)) or {}
        return rec.get("sdm") or (st.get("spec") or {}).get("sdm") or eb

    def _rebuild_rows(self):
        """Lost bridge.json (or a first start on sheets that already have rows): find each track's
        row from the tab's key column, so rows are never duplicated."""
        tabs = self.spec["tracker"]["tabs"]
        ranges = []
        for t in tabs:
            col = sheets.letters(t["columns"])[t["key"]]
            ranges.append(f"{gsheets.a1(t['name'])}!{col}1:{col}{sheets.TRACKER_ROWS}")
        cols = self.tracker.values_get(ranges)
        keys = {self._key(eb): eb for eb in self.ctl.track_ids()}
        for t, grid in zip(tabs, cols):
            for i, row in enumerate(grid[t["header_row"]:], start=t["header_row"] + 1):
                if row and row[0] in keys:
                    self.state["rows"][t["name"]][keys[row[0]]] = i
                elif row and row[0]:
                    self.state["rows"][t["name"]][f"?{row[0]}"] = i   # unknown row: keep its place

    # ---- health ---------------------------------------------------------------------------------
    def _ok(self):
        self.health = {"consecutive_failures": 0, "last_ok": iso(), "error": None}

    def _fail(self, where, e):
        n = self.health.get("consecutive_failures", 0) + 1
        self.health = {**self.health, "consecutive_failures": n, "error": f"{where}: {e}"[:400],
                       "last_error_at": iso()}
        log(f"sheets {where} failed ({n} in a row): {e!r}")
        if not isinstance(e, gsheets.SheetsError):
            log(traceback.format_exc())
        if n in (1, ALARM_AFTER):
            self.ctl.event("-", "sheets", "sheets", f"sheets {where} failed ({n} in a row): {e}")

    def alarm(self):
        if self.health.get("consecutive_failures", 0) >= ALARM_AFTER:
            return self.health["error"]
        if self.drift:
            return f"protection drift: {self.drift[0]}" + (f" (+{len(self.drift) - 1} more)" if len(self.drift) > 1 else "")
        return None

    # ---- ingest ---------------------------------------------------------------------------------
    def watched(self, states: dict | None = None) -> list[str]:
        """Tracks whose flag sheet is read this cycle: calibrating (a verdict there is cleared; the
        first run freezes its rows) or in review. Finished tracks' sheets stay editable but are only
        read again after a reopen. `states` when the caller has them, else state/ on disk."""
        out = []
        for eb, t in self.state["tabs"].items():
            st = (states or {}).get(eb) or read_json(self.r.state(eb)) or {}
            if not t.get("missing") and st.get("stage") in READ_STAGES:
                out.append(eb)
        return out

    def ingest(self):
        self.read, self.clear_m1 = None, {}
        try:
            self._start()
            su = sheets.tab(self.spec, "QA Sign-up")
            signup = self.tracker.values_get([gsheets.a1(su["name"])])[0]
            grids = {}
            for eb in self.watched():
                tab = self.state["tabs"][eb]
                try:
                    grids[eb] = self.book(eb).values_get([f"{gsheets.a1(tab['title'])}!A1:T{self.tb['last_row']}"])[0]
                except gsheets.SheetsError as e:
                    if e.status not in (400, 403, 404):
                        raise
                    tab["missing"] = f"{iso()}: {e}"   # deleted, unshared or renamed: say so, keep going
                    self.ctl.event(eb, "sheets", "sheets", f"flag sheet {tab['title']!r} cannot be read: {e}")
            self.read = {"signup": signup, "tabs": grids}
            self._signups(su, signup)
            for eb, grid in grids.items():
                self._verdict(eb, grid)
            self._save()
        except Exception as e:
            self.read = None
            self._fail("read", e)

    def _signups(self, su, grid):
        if not grid:
            return
        head = [str(h).strip() for h in grid[0]]
        idx = {h: i for i, h in enumerate(head)}

        def get(row, name):
            i = idx.get(name)
            return str(row[i]).strip() if i is not None and i < len(row) else ""
        for eb, r in self.state["rows"][su["name"]].items():
            if eb.startswith("?") or r - 1 >= len(grid):
                continue
            row = grid[r - 1]
            name, email, notes = get(row, "QA Reviewer"), get(row, "Email"), get(row, "Notes")
            prev = self.state["signups"].get(eb) or {}
            if name and name != prev.get("reviewer"):
                self.ctl.event(eb, "sheets", "sheets", f"QA sign-up: {name}"
                               + (f" (was {prev['reviewer']})" if prev.get("reviewer") else ""))
                prev = {"reviewer": name, "signed_up": now_sheet()}
            elif not name:
                prev = {}
            if prev:
                prev.update(email=email, notes=notes)
                self.state["signups"][eb] = prev
            else:
                self.state["signups"].pop(eb, None)

    def _table_rows(self, grid) -> list[dict]:
        names = [c["name"] for c in self.tb["columns"]]
        letters_ = [c["col"] for c in self.tb["columns"]]
        return [{n: _cell(grid, r, L) for n, L in zip(names, letters_)}
                for r in range(self.tb["first_row"], self.tb["last_row"] + 1)]

    def _refant(self, grid, arch) -> tuple[str, str]:
        """(avoid-refant list, problem) from O1."""
        refant = re.sub(r"\s+", "", _cell(grid, 1, "O"))
        if refant and not REFANT_RE.match(refant):
            return "", f"avoid-refant {refant!r}: use ea26 or ea26,ea10"
        if refant and arch.get("antennas") and (bad := sorted(set(refant.split(",")) - set(arch["antennas"]))):
            return "", f"avoid-refant {bad} not in this track"
        return refant, ""

    def _verdict(self, eb, grid):
        raw = _cell(grid, 1, "M")
        word = " ".join(raw.split()).upper()
        if not word or eb in self.state["pending"]:
            return
        st = read_json(self.r.state(eb))
        if not st:
            return
        n = st.get("iteration", 1)
        if word not in VERDICTS:
            self.state["acks"][eb] = f"unknown verdict {raw!r}: use COMPLETE, RESTART, MANUAL REVIEW or HELP REQUESTED"
            return
        if st["stage"] != S.AWAITING_REVIEW:
            self.state["acks"][eb] = (f"{word} not used ({now_sheet()} UTC): the track is {st['stage']} at iteration "
                                      f"{n}, not waiting for review. Set the verdict again once its QA is ready.")
            self.clear_m1[eb] = raw
            self.ctl.event(eb, st["stage"], st["stage"], f"sheet verdict {word} ignored: track is not in review")
            return
        action = VERDICTS[word]
        rows = self._table_rows(grid)
        inputs, applied = {}, []
        if action == "restart":
            arch = archive_ctx(self.r, st, self.ctl.spec(st))
            cmds, probs = flagcmds.build_commands(rows, arch, first_row=self.tb["first_row"])
            refant, head = self._refant(grid, arch)
            if probs or head:
                checks = {}
                for r, msg in probs:
                    checks.setdefault(str(r), []).append(msg)
                self.state["checks"][eb] = {r: "; ".join(m) for r, m in checks.items()}
                where = sorted({r for r, _ in probs})
                self.state["acks"][eb] = (f"RESTART not applied yet ({now_sheet()} UTC): "
                                          + "; ".join(([head] if head else []) + ([f"{len(probs)} problem(s) in row(s) "
                                                                                    f"{', '.join(map(str, where))}; see the Check column"]
                                                                                   if probs else []))
                                          + ". Fix them; the verdict is read again next cycle.")
                return
            inputs["manual_flagging.txt"] = flagcmds.flag_file(arch["sdm"], arch["config"], cmds)
            if refant:
                inputs["refant_ignore.txt"] = refant + "\n"
            applied = [self.tb["first_row"] + i for i, row in enumerate(rows) if flagcmds._truthy(row.get("Apply Row"))]
        self.state["checks"][eb] = {}
        su = self.state["signups"].get(eb) or {}
        d = {"eb": eb, "action": action, "iteration": n, "by": su.get("reviewer") or "flag sheet",
             "ts": iso(), "notes": su.get("notes", ""), "source": "sheets", "verdict_word": word,
             **({"reviewer": su["reviewer"], "signed_up": su.get("signed_up")} if su.get("reviewer") else {})}
        if action == "restart":
            d["inputs"] = inputs
        name = f"sheets-{eb}.i{n}.{iso().replace(':', '')}.json"
        create_json(self.r.inbox / name, d)
        self.state["pending"][eb] = {"file": name, "m1": raw, "verdict": word, "iteration": n, "rows": applied}
        log(f"sheets: {word} for {eb} iteration {n} from {d['by']} -> inbox/{name}")

    # ---- inputs for a run that no verdict froze -------------------------------------------------------
    def run_inputs(self, st) -> str | None:
        """Called by calibrate.submit before the first attempt of an iteration whose inputs were not
        frozen by a RESTART: iteration 1 (the project TEMPLATE's defaults plus anything added since)
        or the run after a reopen (the rows the track ended with, plus edits). Freezes the sheet's
        ticked rows and avoid-refant cell into iter_<n>/inputs/. Returns None once frozen, or why the
        submission waits: a new sheet is made at the end of the cycle the track is queued in, and
        read at the start of the next. Ticked rows that fail the checks for this track are left out
        (shown in Check), not blocking: a project-wide default may not apply to every track."""
        eb, n = st["eb"], st.get("iteration", 1)
        tab = self.state["tabs"].get(eb)
        if not tab:
            return "waiting one cycle for its flag sheet (the run uses its ticked rows)"
        if tab.get("missing"):
            return f"its flag sheet cannot be read ({tab['missing']}); restore it, or have the operator check it"
        grid = (self.read or {}).get("tabs", {}).get(eb)
        if grid is None:
            return ("waiting for the sheets: they could not be read this cycle" if self.read is None
                    else "waiting one cycle to read its flag sheet")
        arch = archive_ctx(self.r, st, self.ctl.spec(st))
        rows = self._table_rows(grid)
        first = self.tb["first_row"]
        cmds, probs = flagcmds.build_commands(rows, arch, first_row=first)
        bad = {r for r, _ in probs}
        used = [first + i for i, row in enumerate(rows) if flagcmds._truthy(row.get("Apply Row")) and first + i not in bad]
        refant, why = self._refant(grid, arch)
        files = {"manual_flagging.txt": flagcmds.flag_file(arch["sdm"], arch["config"], cmds)}
        if refant:
            files["refant_ignore.txt"] = refant + "\n"
        d = self.r.inputs(eb, n)
        d.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            tmp = d / f"partial-{name}"
            tmp.write_text(text)
            os.replace(tmp, d / name)
        checks = {}
        for r, msg in probs:
            checks.setdefault(str(r), []).append(f"not used in iteration {n}: {msg}")
        self.state["checks"][eb] = {r: "; ".join(m) for r, m in checks.items()}
        self.state["stamps"][eb] = {str(r): n for r in used}
        text = (f"iteration {n} started with {len(cmds)} ticked flag row(s)"
                + (f"; {len(bad)} ticked row(s) left out, see the Check column" if bad else "")
                + (f"; avoiding {refant} as refant" if refant else "") + (f"; {why} (ignored)" if why else ""))
        self.state["acks"][eb] = text.replace(" started ", f" started {now_sheet()} UTC ", 1)
        self.ctl.event(eb, st["stage"], st["stage"], f"{text} (frozen in iter_{n}/inputs/)")
        self._save()
        return None

    # ---- publish --------------------------------------------------------------------------------
    def publish(self, states: dict):
        self.cycles += 1
        try:
            self._start()
            self._acknowledge()
            self._books(states)
            self._write(states)
            self._exports(states)              # after the write: the CSV holds the final verdict status
            if self.cycles == 1 or self.cycles % self.cfg.sheets.protection_check_cycles == 0:
                self._check_protection(states)
            self._ok()
        except Exception as e:
            self._fail("write", e)
        finally:
            try:
                self._save()
            finally:
                self._write_status(states)

    def _acknowledge(self):
        writes: dict = {}                       # eb -> [(range, values)] in that track's sheet
        grids = (self.read or {}).get("tabs", {})
        for eb, rows in list(self.state["stamps"].items()):   # rows a run used: "Added in iter" n
            tab, grid = self.state["tabs"].get(eb), grids.get(eb)
            if tab and grid is not None:
                writes.setdefault(eb, []).extend((_a1(tab["title"], "S", int(r)), [[n]])
                                                 for r, n in rows.items() if not _cell(grid, int(r), "S"))
            self.state["stamps"].pop(eb)
        for eb, p in list(self.state["pending"].items()):
            done = read_json(self.r.inbox_archive / p["file"])
            if done is None:
                if (self.r.inbox / p["file"]).exists():
                    continue                     # not processed yet
                self.state["acks"][eb] = f"{p['verdict']} was lost before it was applied; set it again"
                self.state["pending"].pop(eb)
                continue
            res = done.get("result", "")
            if res == "applied":
                self.state["acks"][eb] = f"iteration {p['iteration']}: {p['verdict']} applied {now_sheet()} UTC"
                tab, grid = self.state["tabs"].get(eb), grids.get(eb)
                if p["verdict"] == "RESTART" and tab and grid is not None:
                    writes.setdefault(eb, []).extend((_a1(tab["title"], "S", r), [[p["iteration"] + 1]])
                                                     for r in p["rows"] if not _cell(grid, r, "S"))
            else:
                self.state["acks"][eb] = f"{p['verdict']} not applied: {res}"
            self.clear_m1[eb] = p["m1"]
            self.state["pending"].pop(eb)
        for eb, v in self.clear_m1.items():     # re-read M1 just before clearing it: a newer edit is kept
            tab = self.state["tabs"].get(eb)
            if tab and not tab.get("missing") and _cell(self.book(eb).values_get([_a1(tab["title"], "M", 1)])[0], 1, "A") == v:
                writes.setdefault(eb, []).append((_a1(tab["title"], "M", 1), [[""]]))
        for eb, data in writes.items():
            if data:
                self.book(eb).values_update(data)
        self.clear_m1 = {}

    def _book_name(self, title):
        return f"{self.cfg.name} flags {title}"

    def _books(self, states):
        """A flag spreadsheet for each track that reaches calibration (copied from TEMPLATE)."""
        tabs = self.state["tabs"]
        for eb, st in states.items():
            if st["stage"] in BOOK_STAGES and eb not in tabs:
                self._create_book(eb, st)

    def _exports(self, states):
        """A CSV of a finished track's sheet, taken after this cycle's writes (live, 2026-11-24: taken
        before them, it missed the COMPLETE acknowledgement). Nothing is hidden or deleted."""
        tabs = self.state["tabs"]
        for eb, st in states.items():
            t = tabs.get(eb)
            if st["stage"] in DONE_STAGES and t and not t.get("missing") \
                    and t.get("exported_for") != [st["stage"], st.get("iteration")]:
                self._export(eb)
                t["exported_for"] = [st["stage"], st.get("iteration")]
                self.ctl.event(eb, st["stage"], st["stage"], f"flag sheet {t['title']!r} saved to "
                               f"tracks/{eb}/flag_tab.csv (the sheet stays, readable and editable)")

    def _create_book(self, eb, st):
        title = sheets.flag_tab_name(self.spec, archive_ctx(self.r, st, self.ctl.spec(st)))
        props = {"trackmanager_project": self.cfg.name, "trackmanager_eb": eb}
        found = self.drive.find(props)
        if found:                               # a sheet whose record was lost: adopt it
            sid = found[0]["id"]
            client = self._mk(sid)
            meta = client.meta("sheets(properties(sheetId,title))")
            tab = next((s["properties"] for s in meta["sheets"] if s["properties"]["title"] == title),
                       meta["sheets"][0]["properties"])
            self.state["tabs"][eb] = {"spreadsheet_id": sid, "gid": tab["sheetId"], "title": tab["title"],
                                      "created": iso(), "adopted": True}
            self._save()
            return
        f = self.drive.create_spreadsheet(self._book_name(title), props)
        sid = f["id"]
        self.state["tabs"][eb] = {"spreadsheet_id": sid, "title": title, "created": iso(), "gid": None}
        self._save()                            # recorded before anything else, so it is never made twice
        client = self.book(eb)
        tab = self.flagbook.copy_sheet_to(self.template_gid, sid)
        readme = self.flagbook.copy_sheet_to(self.readme_gid, sid)
        default = [s["properties"]["sheetId"] for s in client.meta("sheets(properties(sheetId,title))")["sheets"]
                   if s["properties"]["sheetId"] not in (tab["sheetId"], readme["sheetId"])]
        client.batch_update([
            {"updateSpreadsheetProperties": {"properties": {"timeZone": sheets_setup.TIME_ZONE}, "fields": "timeZone"}},
            {"updateSheetProperties": {"properties": {"sheetId": tab["sheetId"], "title": title, "hidden": False, "index": 0},
                                       "fields": "title,hidden,index"}},
            {"updateSheetProperties": {"properties": {"sheetId": readme["sheetId"], "title": "README", "hidden": False,
                                                      "index": 1}, "fields": "title,hidden,index"}},
            *({"deleteSheet": {"sheetId": g}} for g in default),
            *sheets.flag_clear_requests(self.spec, tab["sheetId"]),
            *sheets.flag_tab_protection_requests(self.spec, tab["sheetId"], title, [], self.editors),
            sheets._protect(readme["sheetId"], "README", [], self.editors)])
        self.state["tabs"][eb]["gid"] = tab["sheetId"]
        self.ctl.event(eb, st["stage"], st["stage"], f"flag sheet {self._book_name(title)!r} created")
        self._save()

    def _export(self, eb):
        tab = self.state["tabs"][eb]
        grid = self.book(eb).values_get([f"{gsheets.a1(tab['title'])}!A1:T{self.tb['last_row']}"])[0]
        out = self.r.track(eb) / "flag_tab.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(".flag_tab.csv.tmp")
        with open(tmp, "w", newline="") as f:
            csv.writer(f).writerows(grid)
        tmp.replace(out)

    def _url(self, tab):
        return f"https://docs.google.com/spreadsheets/d/{tab['spreadsheet_id']}/edit#gid={tab['gid']}"

    # ---- cell writes ------------------------------------------------------------------------------
    @staticmethod
    def _text(v):
        """Data values are written USER_ENTERED (so dates and numbers are real values); a string
        that would read as a formula is forced to text."""
        if isinstance(v, str) and v.startswith("=") and not v.startswith("=HYPERLINK("):
            return "'" + v
        return v

    def _assign_rows(self, states):
        for t in self.spec["tracker"]["tabs"]:
            rows = self.state["rows"][t["name"]]
            new = [eb for eb in states if eb not in rows]
            new.sort(key=lambda eb: ((read_json(self.r.archive_json(eb)) or {}).get("start") or 1e9, eb))
            nxt = max(list(rows.values()) + [t["header_row"]]) + 1
            for eb in new:
                if nxt > sheets.TRACKER_ROWS:
                    raise gsheets.SheetsError(f"{t['name']} is full ({sheets.TRACKER_ROWS} rows)")
                rows[eb], nxt = nxt, nxt + 1

    def _write(self, states):
        self._assign_rows(states)
        jobs = read_json(self.r.jobs_summary) or {}
        want = {"T": {}}                         # "T" = tracker; else a track's spreadsheet id
        for eb, st in states.items():
            tab = self.state["tabs"].get(eb)
            ok = tab and tab.get("gid") is not None and not tab.get("missing")
            ctx = track_context(self.r, self.cfg, st, jobs, {
                "signup": self.state["signups"].get(eb), "ack": self.state["acks"].get(eb, ""),
                "tab_url": self._url(tab) if ok else ""}, self.ctl.spec(st))
            for t in self.spec["tracker"]["tabs"]:
                row = self.state["rows"][t["name"]][eb]
                lm = sheets.letters(t["columns"])
                plain = {c["name"] for c in t["columns"] if c["source"] != "formula" and not c.get("link_text")}
                for name, v in sheets.tracker_row(t, ctx, row, existing={}).items():
                    want["T"][f"{t['name']}|{lm[name]}{row}"] = self._text(v) if name in plain else v
            if not ok:
                continue
            w = want.setdefault(tab["spreadsheet_id"], {})
            for a1, v in sheets.flag_tab_cells(self.spec, ctx).items():
                w[f"{tab['title']}|{a1}"] = self._text(v)
            # Check column: a row's problems, or "" where the cell still shows an old message
            grid = (self.read or {}).get("tabs", {}).get(eb)
            for r in range(self.tb["first_row"], self.tb["last_row"] + 1):
                msg = (self.state["checks"].get(eb) or {}).get(str(r), "")
                key = f"{tab['title']}|T{r}"
                if msg or self.mirror.get(f"{tab['spreadsheet_id']}|{key}") or (grid is not None and _cell(grid, r, "T")):
                    w[key] = msg
        for which, cells in want.items():
            changed = {k: v for k, v in cells.items()
                       if f"{which}|{k}" not in self.mirror or self.mirror[f"{which}|{k}"] != v}
            if not changed:
                continue
            client = self.tracker if which == "T" else self.books.get(which) or self._mk(which)
            client.values_update(self._runs(changed))
            self.mirror.update({f"{which}|{k}": v for k, v in changed.items()})

    @staticmethod
    def _runs(cells: dict) -> list:
        """{"Tab|C7": v, ...} -> value ranges, one per run of adjacent cells in a row."""
        by = {}
        for key, v in cells.items():
            tab, a1 = key.split("|", 1)
            m = re.fullmatch(r"([A-Z]+)(\d+)", a1)
            by.setdefault((tab, int(m.group(2))), {})[sheets.col_index(m.group(1))] = v
        out = []
        for (tab, row), cols in sorted(by.items()):
            run = []
            for c in sorted(cols):
                if run and c != run[-1] + 1:
                    out.append((f"{gsheets.a1(tab)}!{sheets.col_letter(run[0])}{row}", [[cols[x] for x in run]]))
                    run = []
                run.append(c)
            out.append((f"{gsheets.a1(tab)}!{sheets.col_letter(run[0])}{row}", [[cols[x] for x in run]]))
        return out

    # ---- protection drift --------------------------------------------------------------------------
    def _check_protection(self, states):
        fields = "sheets(properties(sheetId,title,hidden),protectedRanges(range,editors,warningOnly,unprotectedRanges))"
        probs = sheets_setup.check(self.spec, self.tracker.meta(fields), "tracker", self.editors)
        probs += sheets_setup.check(self.spec, self.flagbook.meta(fields), "flagbook", self.editors)
        tmpl = self.fb["template_tab"]       # a verdict typed into TEMPLATE is never read or copied: say so
        typed = sheets.template_input(self.spec, self.flagbook.values_get(
            [f"{gsheets.a1(tmpl)}!A1:T{self.tb['last_row']}"])[0])
        if typed:
            probs.append(f"flagbook/{tmpl}: has input in {', '.join(typed[:6])}{' ...' if len(typed) > 6 else ''}"
                         " (verdict, avoid-refant or bridge cells); it is never read or copied to a track's sheet. "
                         "Run init --sheets with the controller stopped to clear it")
        for eb in self.watched(states):      # the sheets reviewers are working in now
            t = self.state["tabs"][eb]
            meta = self.book(eb).meta(fields)
            sh = next((s for s in meta["sheets"] if s["properties"]["sheetId"] == t["gid"]), None)
            if sh is None:
                probs.append(f"{self._book_name(t['title'])}: its flag tab is missing")
                continue
            probs += sheets_setup.check_tab(sh, self._book_name(t["title"]),
                                            self.fb["protection"]["editable_ranges"], self.editors)
        probs += [f"{self._book_name(t['title'])}: cannot be read ({t['missing']})"
                  for t in self.state["tabs"].values() if t.get("missing")]
        if probs != self.drift:
            for p in sorted(set(probs) - set(self.drift)):
                self.ctl.event("-", "sheets", "sheets", f"protection drift: {p}")
            if self.drift and not probs:
                self.ctl.event("-", "sheets", "sheets", "protections match the spec again")
        self.drift = probs
        self.health["protection_checked"] = iso()

    # ---- status -----------------------------------------------------------------------------------
    def needs_you(self, states) -> list[dict]:
        out = []
        rv = self.cfg.review
        for eb, st in sorted(states.items()):
            su = self.state["signups"].get(eb) or {}
            if su.get("reviewer") and not sheets.EMAIL_RE.match(su.get("email", "")):
                out.append({"eb": eb, "item": "signup_without_email",
                            "detail": f"{su['reviewer']} signed up without a valid email (no 'QA ready' email can be sent)"})
            if st["stage"] == S.AWAITING_REVIEW:
                if not su.get("reviewer"):
                    out.append({"eb": eb, "item": "ready_without_reviewer", "detail": "ready for review; nobody signed up"})
                age = age_s(st.get("updated"))
                if age is not None and age > rv.stale_after_h * 3600:
                    out.append({"eb": eb, "item": "stale_review",
                                "detail": f"waiting for review for {age / 3600:.0f} h (stale_after_h {rv.stale_after_h:g})"})
            if st.get("iteration", 1) > rv.escalate_after_iterations:
                out.append({"eb": eb, "item": "many_iterations",
                            "detail": f"iteration {st['iteration']} (escalate_after_iterations {rv.escalate_after_iterations})"})
            if st.get("hold") and st.get("hold_reason") in ("manual_review", "help_requested"):
                out.append({"eb": eb, "item": "reviewer_hold",
                            "detail": f"{st['hold_reason'].replace('_', ' ')} from {su.get('reviewer') or 'the reviewer'}"})
            if self.state["checks"].get(eb):
                out.append({"eb": eb, "item": "flag_rows_have_problems", "detail": self.state["acks"].get(eb, "")})
            t = self.state["tabs"].get(eb)
            if t and t.get("missing"):
                out.append({"eb": eb, "item": "flag_sheet_missing", "detail": t["missing"]})
        return out

    def _write_status(self, states):
        status = {"ts": iso(), "service_account": self.sa, "health": self.health, "drift": self.drift,
                  "alarm": self.alarm(), "needs_you": self.needs_you(states),
                  "tracker_id": self.cfg.sheets.tracker_id, "flagbook_id": self.cfg.sheets.flagbook_id,
                  "flags_folder_id": self.cfg.sheets.flags_folder_id,
                  "flag_sheets": len(self.state["tabs"]), "pending_verdicts": sorted(self.state["pending"])}
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json(self.dir / "status.json", status)
        return status
