"""Sheets bridge (plan §3.5, M4): a synchronous step of the controller's cycle that mirrors the
track database to the tracker and flag book, and reads reviewers' sign-ups and verdicts back.

    bridge.ingest()        before inbox.apply: read the sign-up columns and every live flag tab;
                           stamp sign-ups; turn a valid verdict (M1) into inbox/decisions/sheets-*.json
                           (RESTART carries the flag file rebuilt from ALL ticked rows by flagcmds,
                           plus O1 as refant_ignore.txt); a verdict with problems stays in M1, with
                           the problems in the row's Check cell and a summary in M2
    bridge.publish(states) after the summaries: acknowledge processed verdicts (M2, clear M1,
                           stamp "Added in iter"), create a flag tab when a track first calibrates,
                           export/hide/delete it after DONE, write every changed controller cell in
                           one values batch per spreadsheet, check protections every N cycles

The controller stays the only writer of state/: the bridge only writes inbox/ and its own
<root>/sheets/ files:
  bridge.json   spreadsheet ids, row of each track per tracker tab, flag tabs, sign-ups, acks,
                per-row problems, verdicts waiting for the controller
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
TAB_STAGES = (S.CALIBRATING, S.AWAITING_REVIEW, S.PROMOTING)   # a track gets its flag tab here
DONE_STAGES = (S.DONE, S.SKIPPED, S.NEEDS_MANUAL)               # ... and loses it after these
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
    def __init__(self, ctl, tracker=None, flagbook=None):
        self.ctl, self.cfg, self.r = ctl, ctl.cfg, ctl.r
        self.dir = ctl.r.path / "sheets"
        self.tracker, self.flagbook = tracker, flagbook
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
    def _load(self):
        st = read_json(self.dir / "bridge.json") or {}
        if (st.get("tracker_id"), st.get("flagbook_id")) != (self.cfg.sheets.tracker_id, self.cfg.sheets.flagbook_id):
            st = {}                      # new spreadsheets: start over (rows, tabs, mirror)
            self.mirror_reset = True
        else:
            self.mirror_reset = False
        st.update(tracker_id=self.cfg.sheets.tracker_id, flagbook_id=self.cfg.sheets.flagbook_id)
        for key in ("rows", "tabs", "signups", "acks", "checks", "pending"):
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
        if self.tracker is not None:
            return
        fake = os.environ.get("TM_FAKE_SHEETS_DIR")
        if fake:                         # tests: a file-backed FakeSheets shared with the test process
            from .fake_sheets import FileFakeSheets
            mk = lambda sid: FileFakeSheets(Path(fake) / f"{sid}.pkl", spreadsheet_id=sid)
        else:
            mk = lambda sid: gsheets.SheetsClient(sid, credentials=self.cfg.sheets_key)
        self.tracker, self.flagbook = mk(self.cfg.sheets.tracker_id), mk(self.cfg.sheets.flagbook_id)

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
        if self.fb["template_tab"] not in ftitles:
            missing.append(f"flag book {self.fb['template_tab']}")
        if missing:
            raise gsheets.SheetsError(f"spreadsheet tabs missing ({', '.join(missing)}): run "
                                      "python -m trackmanager.init --project <project.yaml> --sheets")
        self.template_gid = ftitles[self.fb["template_tab"]]["sheetId"]
        gids = {p["sheetId"]: t for t, p in ftitles.items()}
        for eb, tab in self.state["tabs"].items():   # a tab deleted by hand: forget it
            if not tab.get("dropped_at") and tab.get("gid") not in gids:
                tab["dropped_at"] = iso()
                self.ctl.event(eb, "sheets", "sheets", f"flag tab {tab['title']!r} is gone from the flag book")
        if not any(self.state["rows"].values()):
            self._rebuild_rows()
        self.started = True
        log(f"sheets bridge started: service account {self.sa}; tracker {self.cfg.sheets.tracker_id}, "
            f"flag book {self.cfg.sheets.flagbook_id}")

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
    def live_tabs(self):
        return {eb: t for eb, t in self.state["tabs"].items() if not t.get("hidden_at") and not t.get("dropped_at")}

    def ingest(self):
        self.read, self.clear_m1 = None, {}
        try:
            self._start()
            su = sheets.tab(self.spec, "QA Sign-up")
            signup = self.tracker.values_get([gsheets.a1(su["name"])])[0]
            live = self.live_tabs()
            grids = self.flagbook.values_get(
                [f"{gsheets.a1(t['title'])}!A1:T{self.tb['last_row']}" for t in live.values()]) if live else []
            self.read = {"signup": signup, "tabs": dict(zip(live, grids))}
            self._signups(su, signup)
            for eb in live:
                self._verdict(eb, self.read["tabs"][eb])
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
        rows = []
        for r in range(self.tb["first_row"], self.tb["last_row"] + 1):
            rows.append({n: _cell(grid, r, L) for n, L in zip(names, letters_)})
        return rows

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
            refant = re.sub(r"\s+", "", _cell(grid, 1, "O"))
            head = []
            if refant and not REFANT_RE.match(refant):
                head.append(f"avoid-refant {refant!r}: use ea26 or ea26,ea10")
            elif refant and arch.get("antennas") and (bad := sorted(set(refant.split(",")) - set(arch["antennas"]))):
                head.append(f"avoid-refant {bad} not in this track")
            if probs or head:
                checks = {}
                for r, msg in probs:
                    checks.setdefault(str(r), []).append(msg)
                self.state["checks"][eb] = {r: "; ".join(m) for r, m in checks.items()}
                where = sorted({r for r, _ in probs})
                self.state["acks"][eb] = (f"RESTART not applied yet ({now_sheet()} UTC): "
                                          + "; ".join(head + ([f"{len(probs)} problem(s) in row(s) "
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
        d = {"eb": eb, "action": action, "iteration": n, "by": su.get("reviewer") or "flag book",
             "ts": iso(), "notes": su.get("notes", ""), "source": "sheets", "verdict_word": word,
             **({"reviewer": su["reviewer"], "signed_up": su.get("signed_up")} if su.get("reviewer") else {})}
        if action == "restart":
            d["inputs"] = inputs
        name = f"sheets-{eb}.i{n}.{iso().replace(':', '')}.json"
        create_json(self.r.inbox / name, d)
        self.state["pending"][eb] = {"file": name, "m1": raw, "verdict": word, "iteration": n, "rows": applied}
        log(f"sheets: {word} for {eb} iteration {n} from {d['by']} -> inbox/{name}")

    # ---- publish --------------------------------------------------------------------------------
    def publish(self, states: dict):
        self.cycles += 1
        try:
            self._start()
            self._acknowledge()
            self._tabs(states)
            self._write(states)
            if self.cycles == 1 or self.cycles % self.cfg.sheets.protection_check_cycles == 0:
                self._check_protection()
            self._ok()
        except Exception as e:
            self._fail("write", e)
        finally:
            try:
                self._save()
            finally:
                self._write_status(states)

    def _acknowledge(self):
        stamps = []
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
                tab = self.state["tabs"].get(eb)
                grid = (self.read or {}).get("tabs", {}).get(eb)
                if p["verdict"] == "RESTART" and tab and grid is not None:
                    for r in p["rows"]:              # rows a restart first used: "Added in iter"
                        if not _cell(grid, r, "S"):
                            stamps.append((_a1(tab["title"], "S", r), [[p["iteration"] + 1]]))
            else:
                self.state["acks"][eb] = f"{p['verdict']} not applied: {res}"
            self.clear_m1[eb] = p["m1"]
            self.state["pending"].pop(eb)
        clear = {eb: v for eb, v in self.clear_m1.items() if self.state["tabs"].get(eb)
                 and not self.state["tabs"][eb].get("dropped_at")}
        if clear:     # re-read M1 just before clearing it: a reviewer's newer edit is never erased
            now = self.flagbook.values_get([_a1(self.state["tabs"][eb]["title"], "M", 1) for eb in clear])
            for (eb, v), grid in zip(clear.items(), now):
                if _cell(grid, 1, "A") == v:
                    stamps.append((_a1(self.state["tabs"][eb]["title"], "M", 1), [[""]]))
        if stamps:
            self.flagbook.values_update(stamps)
        self.clear_m1 = {}

    def _tab_url(self, tab):
        return f"https://docs.google.com/spreadsheets/d/{self.cfg.sheets.flagbook_id}/edit#gid={tab['gid']}"

    def _tabs(self, states):
        tabs = self.state["tabs"]
        create = [eb for eb, st in states.items() if st["stage"] in TAB_STAGES and eb not in tabs]
        hide = [eb for eb, st in states.items() if st["stage"] in DONE_STAGES and eb in tabs
                and not tabs[eb].get("hidden_at") and not tabs[eb].get("dropped_at")]
        keep_d = self.cfg.sheets.drop_done_tabs_after_d
        drop = [eb for eb, t in tabs.items() if t.get("hidden_at") and not t.get("dropped_at")
                and (age_s(t["hidden_at"]) or 0) >= keep_d * 86400]
        if create:
            meta = self.flagbook.meta("sheets(properties(sheetId,title))")
            titles = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
            reqs, new = [], []
            used = {t["title"] for t in tabs.values() if not t.get("dropped_at")}
            ids = {s["properties"]["sheetId"] for s in meta["sheets"]}
            next_id = max([10_000] + [i for i in ids if i < 2_000_000_000]) + 1
            for eb in create:
                title = sheets.flag_tab_name(self.spec, archive_ctx(self.r, states[eb], self.ctl.spec(states[eb])))
                if title in titles and title not in used:        # adopt a tab whose record was lost
                    tabs[eb] = {"title": title, "gid": titles[title], "created": iso(), "adopted": True}
                    continue
                if title in used:
                    title = f"{title}_{len(tabs)}"
                while next_id in ids:
                    next_id += 1
                reqs += sheets.flag_tab_copy_requests(self.spec, self.template_gid, next_id, title,
                                                      len(titles) + len(new))
                new.append((eb, title, next_id))
                ids.add(next_id)
            if reqs:   # copy TEMPLATE, show the copy, empty its reviewer cells; then set its protection
                self.flagbook.batch_update(reqs)
                for eb, title, gid in new:
                    tabs[eb] = {"title": title, "gid": gid, "created": iso()}
                    self.ctl.event(eb, states[eb]["stage"], states[eb]["stage"], f"flag tab {title!r} created")
                self._save()
                self._protect_new([tabs[eb] for eb, _, _ in new])
        reqs = []
        for eb in hide:
            self._export(eb)
            reqs.append({"updateSheetProperties": {"properties": {"sheetId": tabs[eb]["gid"], "hidden": True},
                                                   "fields": "hidden"}})
        for eb in drop:
            reqs.append({"deleteSheet": {"sheetId": tabs[eb]["gid"]}})
        if reqs:
            self.flagbook.batch_update(reqs)
            for eb in hide:
                tabs[eb]["hidden_at"] = iso()
                self.ctl.event(eb, states[eb]["stage"], states[eb]["stage"],
                               f"flag tab {tabs[eb]['title']!r} exported to tracks/{eb}/flag_tab.csv and hidden")
            for eb in drop:
                tabs[eb]["dropped_at"] = iso()
                self.mirror = {k: v for k, v in self.mirror.items() if not k.startswith(f"F|{tabs[eb]['title']}|")}
                self.ctl.event(eb, "sheets", "sheets", f"flag tab {tabs[eb]['title']!r} deleted "
                               f"({keep_d:g} d after it was hidden; the CSV and frozen inputs/ remain)")

    def _protect_new(self, new_tabs):
        """A copy of TEMPLATE carries TEMPLATE's full lock: replace it with the track-tab protection
        (reviewer ranges editable)."""
        meta = self.flagbook.meta("sheets(properties(sheetId,title),protectedRanges(protectedRangeId))")
        by = {s["properties"]["sheetId"]: s for s in meta["sheets"]}
        reqs = []
        for t in new_tabs:
            old = [p["protectedRangeId"] for p in by.get(t["gid"], {}).get("protectedRanges", [])]
            reqs += sheets.flag_tab_protection_requests(self.spec, t["gid"], t["title"], old, self.editors)
        if reqs:
            self.flagbook.batch_update(reqs)

    def _export(self, eb):
        grid = (self.read or {}).get("tabs", {}).get(eb)
        if grid is None:
            grid = self.flagbook.values_get([f"{gsheets.a1(self.state['tabs'][eb]['title'])}!A1:T{self.tb['last_row']}"])[0]
        out = self.r.track(eb) / "flag_tab.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(".flag_tab.csv.tmp")
        with open(tmp, "w", newline="") as f:
            csv.writer(f).writerows(grid)
        tmp.replace(out)

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
        want = {"T": {}, "F": {}}
        for eb, st in states.items():
            tab = self.state["tabs"].get(eb)
            live_tab = tab and not tab.get("dropped_at")
            ctx = track_context(self.r, self.cfg, st, jobs, {
                "signup": self.state["signups"].get(eb), "ack": self.state["acks"].get(eb, ""),
                "tab_url": self._tab_url(tab) if live_tab else ""}, self.ctl.spec(st))
            for t in self.spec["tracker"]["tabs"]:
                row = self.state["rows"][t["name"]][eb]
                lm = sheets.letters(t["columns"])
                plain = {c["name"] for c in t["columns"] if c["source"] not in ("formula",) and not c.get("link_text")}
                for name, v in sheets.tracker_row(t, ctx, row, existing={}).items():
                    want["T"][f"{t['name']}|{lm[name]}{row}"] = self._text(v) if name in plain else v
            if live_tab:
                for a1, v in sheets.flag_tab_cells(self.spec, ctx).items():
                    want["F"][f"{tab['title']}|{a1}"] = self._text(v)
                # Check column: a row's problems, or "" where the cell still shows an old message
                grid = (self.read or {}).get("tabs", {}).get(eb)
                for r in range(self.tb["first_row"], self.tb["last_row"] + 1):
                    msg = (self.state["checks"].get(eb) or {}).get(str(r), "")
                    key = f"{tab['title']}|T{r}"
                    if msg or self.mirror.get(f"F|{key}") or (grid is not None and _cell(grid, r, "T")):
                        want["F"][key] = msg
        for which, client in (("T", self.tracker), ("F", self.flagbook)):
            changed = {k: v for k, v in want[which].items()
                       if f"{which}|{k}" not in self.mirror or self.mirror[f"{which}|{k}"] != v}
            if not changed:
                continue
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
    def _check_protection(self):
        fields = "sheets(properties(sheetId,title,hidden),protectedRanges(range,editors,warningOnly,unprotectedRanges))"
        probs = sheets_setup.check(self.spec, self.tracker.meta(fields), "tracker", self.editors)
        fmeta = self.flagbook.meta(fields)
        probs += sheets_setup.check(self.spec, fmeta, "flagbook", self.editors)
        by_gid = {s["properties"]["sheetId"]: s for s in fmeta["sheets"]}
        for eb, t in self.state["tabs"].items():
            if t.get("dropped_at"):
                continue
            if t["gid"] not in by_gid:
                probs.append(f"flagbook/{t['title']}: the flag tab of {eb} is missing")
                continue
            probs += sheets_setup.check_tab(by_gid[t["gid"]], f"flagbook/{t['title']}",
                                            self.fb["protection"]["editable_ranges"], self.editors)
        tmpl = self.fb["template_tab"]       # input typed into TEMPLATE is never read: say so
        typed = sheets.reviewer_input(self.spec, self.flagbook.values_get(
            [f"{gsheets.a1(tmpl)}!A1:T{self.tb['last_row']}"])[0])
        if typed:
            probs.append(f"flagbook/{tmpl}: has reviewer input in {', '.join(typed[:6])}"
                         f"{' ...' if len(typed) > 6 else ''}; it is never read (use the track's own tab). "
                         "Run init --sheets with the controller stopped to clear it")
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
                out.append({"eb": eb, "item": "verdict_has_problems", "detail": self.state["acks"].get(eb, "")})
        return out

    def _write_status(self, states):
        status = {"ts": iso(), "service_account": self.sa, "health": self.health, "drift": self.drift,
                  "alarm": self.alarm(), "needs_you": self.needs_you(states),
                  "tracker_id": self.cfg.sheets.tracker_id, "flagbook_id": self.cfg.sheets.flagbook_id,
                  "flag_tabs": sum(1 for t in self.state["tabs"].values() if not t.get("dropped_at")),
                  "pending_verdicts": sorted(self.state["pending"])}
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json(self.dir / "status.json", status)
        return status
