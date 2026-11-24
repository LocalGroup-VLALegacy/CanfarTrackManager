"""Build and check the two spreadsheets' tab structure from spec v1 (plan §3.5; `init --sheets`).

The operator creates two empty spreadsheets and shares them with the service account as Editor.
build() then turns each into the managed tabs, in one atomic batchUpdate per spreadsheet:

  tracker   Track Summary, QA Sign-up, _meta (hidden)
  flag book README, TEMPLATE (copied per EB by the bridge), _meta (hidden)

It is rerunnable: a tab that already exists is left alone and only checked, so build() never
touches rows or reviewer input. Rows and per-EB flag tabs are written by the controller's Sheets
bridge, the one writer of sheet content. check() compares the protections with the spec; the
bridge reuses it to raise an alarm on drift (without changing anything).
"""
from __future__ import annotations

from . import qa, sheets
from .store import iso

TIME_ZONE = "Etc/GMT"      # stamps are written in UTC; the sheet's own clock should agree
SHEET_IDS = {"tracker": {"Track Summary": 101, "QA Sign-up": 102, "_meta": 199},
             "flagbook": {"README": 201, "TEMPLATE": 202, "_meta": 299}}


def project_values(cfg) -> dict:
    """The ${...} values the spec needs, from a ProjectConfig."""
    return {"project": {"name": cfg.name},
            "review": {"email_prefix": (cfg.review.email_prefix or "QA").strip().strip("[]")},
            "sheets": {"links": dict(cfg.sheets.links or {}), "editors": cfg.sheets.editors},
            "qa": {"home_url": qa.url_for(cfg.qa.url_base, cfg.products_dir / "index.html") or ""}}


def editors_for(cfg, service_account: str) -> list[str]:
    return sheets.protection_editors(project_values(cfg), service_account)


def expected_tabs(spec, which: str, editors: list[str]) -> dict:
    """title -> (sheetId, request builder(sheet_id) -> requests, unprotected A1 ranges)."""
    def meta_tab(sid):
        return _meta_requests(sid, "_meta", editors)
    if which == "tracker":
        out = {}
        for t in spec["tracker"]["tabs"]:
            lm = sheets.letters(t["columns"])
            hr = t["header_row"]
            un = [f"{lm[n]}{hr + 1}:{lm[n]}{sheets.TRACKER_ROWS}" for n in t["protection"]["editable_columns"]]
            out[t["name"]] = (SHEET_IDS[which][t["name"]],
                              lambda sid, n=t["name"]: sheets.tracker_tab_requests(spec, n, sid, editors), un)
        out["_meta"] = (SHEET_IDS[which]["_meta"], meta_tab, [])
        return out
    fb = spec["flagbook"]
    readme = next(t for t in fb.get("extra_tabs", []) if t["name"] == "README")
    return {"README": (SHEET_IDS[which]["README"], lambda sid: _readme_requests(sid, readme, editors), []),
            fb["template_tab"]: (SHEET_IDS[which]["TEMPLATE"],
                                 lambda sid: sheets.flag_template_requests(spec, sid, editors),
                                 []),                               # hidden and fully locked
            "_meta": (SHEET_IDS[which]["_meta"], meta_tab, [])}


def _text_rows(sid, rows: list[list[str]], bold_first_col=False) -> dict:
    return {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                            "rows": [{"values": [{"userEnteredValue": {"stringValue": str(v)},
                                                  **({"userEnteredFormat": {"textFormat": {"bold": True}}}
                                                     if bold_first_col and j == 0 else {})}
                                                 for j, v in enumerate(r)]} for r in rows],
                            "fields": "userEnteredValue,userEnteredFormat(textFormat)"}}


def _readme_requests(sid, readme, editors):
    lines = [[ln] for ln in readme["text"].rstrip("\n").splitlines()]
    return [{"updateSheetProperties": {"properties": {"sheetId": sid, "title": "README"}, "fields": "title"}},
            _text_rows(sid, lines),
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0,
                                                     "endIndex": 1}, "properties": {"pixelSize": 900},
                                           "fields": "pixelSize"}},
            sheets._protect(sid, "README", [], editors)]


def _meta_requests(sid, title, editors):
    return [{"updateSheetProperties": {"properties": {"sheetId": sid, "title": title, "hidden": True},
                                       "fields": "title,hidden"}},
            sheets._protect(sid, title, [], editors)]


def meta_values(spec, cfg, service_account) -> list[list]:
    return [["Managed by trackmanager (CanfarTrackManager); do not edit this tab."],
            ["spec_version", spec.get("version", 1)], ["project", cfg.name],
            ["service_account", service_account], ["built", iso()]]


def build(cfg, client, which: str, service_account: str, spec=None) -> dict:
    """Create the missing managed tabs of one spreadsheet. Returns a report dict
    (created, existing, deleted, problems). Sends at most one batchUpdate plus one values write."""
    spec = spec or sheets.load_spec(sheets.SPEC, project_values(cfg))
    editors = editors_for(cfg, service_account)
    meta = client.meta("properties(title,timeZone),sheets(properties(sheetId,title,index),protectedRanges)")
    have = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}
    ids = {p["sheetId"]: t for t, p in have.items()}
    want = expected_tabs(spec, which, editors)
    reqs, created = [], []
    if meta.get("properties", {}).get("timeZone") != TIME_ZONE:
        reqs.append({"updateSpreadsheetProperties": {"properties": {"timeZone": TIME_ZONE}, "fields": "timeZone"}})
    for i, (title, (sid, builder, _)) in enumerate(want.items()):
        if title in have:
            continue
        if sid in ids:
            raise RuntimeError(f"{which}: sheetId {sid} is taken by tab {ids[sid]!r}; rename or remove that tab")
        reqs.append({"addSheet": {"properties": {"sheetId": sid, "title": title, "index": i}}})
        reqs += builder(sid)
        created.append(title)
    repaired = []
    tmpl = spec["flagbook"]["template_tab"]
    if which == "flagbook" and tmpl in have and tmpl not in created:
        repaired, fix = _repair_template(spec, client, meta, editors)
        reqs += fix
    deleted = []
    if created and "Sheet1" in have and not client.values_get([f"'Sheet1'!A1:Z50"])[0]:
        reqs.append({"deleteSheet": {"sheetId": have["Sheet1"]["sheetId"]}})   # the empty default tab
        deleted.append("Sheet1")
    if reqs:
        client.batch_update(reqs)
    if "_meta" in created:
        client.values_update([("'_meta'!A1", meta_values(spec, cfg, service_account))])
    after = client.meta("sheets(properties(sheetId,title,hidden),protectedRanges)")
    return {"spreadsheet": client.id, "which": which, "created": created, "repaired": repaired,
            "existing": [t for t in want if t not in created], "deleted": deleted,
            "editors": editors, "problems": check(spec, after, which, editors)}


def _repair_template(spec, client, meta, editors):
    """An existing TEMPLATE from before it was hidden and locked to operators, or one with a verdict,
    avoid-refant, Added in iter or Check typed into it: the requests that hide it, lock it and
    clear those cells. Its flag rows (the project's defaults) are kept."""
    title = spec["flagbook"]["template_tab"]
    sh = next(s for s in meta["sheets"] if s["properties"]["title"] == title)
    sid = sh["properties"]["sheetId"]
    full = client.meta("sheets(properties(sheetId,title,hidden),protectedRanges)")
    sh = next(s for s in full["sheets"] if s["properties"]["sheetId"] == sid)
    done, reqs = [], []
    if check_tab(sh, f"flagbook/{title}", [], editors):
        reqs += [{"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}}
                 for p in sh.get("protectedRanges", [])]
        reqs.append(sheets._protect(sid, title, [], editors))
        done.append("protection: fully locked")
    if not sh["properties"].get("hidden"):
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "hidden": True}, "fields": "hidden"}})
        done.append("hidden")
    last = spec["flagbook"]["table"]["last_row"]
    typed = sheets.template_input(spec, client.values_get([f"'{title}'!A1:T{last}"])[0])
    if typed:
        reqs += sheets.flag_clear_requests(spec, sid)
        done.append(f"cleared {', '.join(typed[:8])}" + (" ..." if len(typed) > 8 else "")
                    + " (never copied to a track's tab)")
    return done, reqs


def _rng_key(g: dict) -> tuple:
    return (g.get("startRowIndex", 0), g.get("endRowIndex"), g.get("startColumnIndex", 0), g.get("endColumnIndex"))


def check_tab(sh: dict, where: str, unprotected: list[str], editors: list[str]) -> list[str]:
    """One tab: exactly one whole-tab protection (not warning-only) whose editors are the expected
    ones (extra editors are drift too: someone widened the lock) and whose unprotected ranges are
    `unprotected` (A1 ranges)."""
    probs = []
    whole = [p for p in sh.get("protectedRanges", []) if set(p.get("range", {})) == {"sheetId"}]
    if len(whole) != 1:
        return [f"{where}: expected one whole-tab protection, found {len(whole)}"]
    p = whole[0]
    if p.get("warningOnly"):
        probs.append(f"{where}: protection is warning-only")
    want = {e.lower() for e in editors}
    got = {e.lower() for e in p.get("editors", {}).get("users", [])}
    if missing := sorted(want - got):
        probs.append(f"{where}: protection editors missing {missing}")
    if extra := sorted(got - want):
        probs.append(f"{where}: protection has extra editors {extra}")
    if p.get("editors", {}).get("groups") or p.get("editors", {}).get("domainUsersCanEdit"):
        probs.append(f"{where}: protection is open to a group or domain")
    sid = sh["properties"]["sheetId"]
    if {_rng_key(sheets._grid(sid, a)) for a in unprotected} != {_rng_key(u) for u in p.get("unprotectedRanges", [])}:
        probs.append(f"{where}: unprotected ranges differ from the spec")
    return probs


def check(spec, meta: dict, which: str, editors: list[str]) -> list[str]:
    """Protection drift for the managed tabs of one spreadsheet (empty list = as specified)."""
    probs = []
    by_title = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
    for title, (_, _, unprotected) in expected_tabs(spec, which, editors).items():
        if title not in by_title:
            probs.append(f"{which}: tab {title!r} is missing")
            continue
        probs += check_tab(by_title[title], f"{which}/{title}", unprotected, editors)
        if title == "_meta" and not by_title[title]["properties"].get("hidden"):   # operators unhide TEMPLATE to edit it
            probs.append(f"{which}/{title}: should be hidden")
    return probs
