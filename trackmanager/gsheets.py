"""Minimal Google Sheets API v4 client for the controller (plan §3.5, M4).

Four calls are all the bridge needs, and trackmanager/sheets.py already builds the raw v4 request
bodies, so this sits directly on google-auth's AuthorizedSession instead of gspread:

    c = SheetsClient(spreadsheet_id, credentials="/path/key.json")
    c.meta("sheets(properties,protectedRanges)")        spreadsheets.get with a fields mask
    c.batch_update([...requests...])                    spreadsheets.batchUpdate -> replies
    c.values_get(["'Tab'!A1:C9"])                       values:batchGet -> [[row], ...] per range
    c.values_update([("'Tab'!A2", [[1, 2]])])           values:batchUpdate (USER_ENTERED)

Scope is spreadsheets only (no Drive): sheets are opened by id and shared by hand. 429 and 5xx
answers, and connection errors, are retried with exponential backoff; anything else raises
SheetsError at once. trackmanager.fake_sheets.FakeSheets has the same methods for tests.
"""
from __future__ import annotations

import json
import os
import random
import stat
import time
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"
RETRY_STATUS = {429, 500, 502, 503, 504}


class SheetsError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class KeyFileError(ValueError):
    """The service-account key file is missing, unreadable, too open, or not a service account."""


def check_key(path) -> dict:
    """Validate the service-account key file and return {client_email, project_id}. Refuses a
    file that group or others can read: the key grants edit access to every shared sheet."""
    p = Path(os.path.expanduser(str(path)))
    try:
        st = p.stat()
    except OSError as e:
        raise KeyFileError(f"service-account key {p}: {e.strerror or e}") from e
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFileError(f"service-account key {p} is readable by group or others "
                        f"(mode {stat.filemode(st.st_mode)}); run: chmod 600 {p}")
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise KeyFileError(f"service-account key {p}: not readable JSON ({e})") from e
    if d.get("type") != "service_account" or not d.get("client_email") or not d.get("private_key"):
        raise KeyFileError(f"service-account key {p}: not a service-account key (type {d.get('type')!r})")
    return {"client_email": d["client_email"], "project_id": d.get("project_id")}


def service_account_email(path) -> str:
    return check_key(path)["client_email"]


def a1(tab: str, rng: str | None = None) -> str:
    """'Tab name'!A1:B2 with the tab name quoted (quotes in names doubled)."""
    q = "'" + tab.replace("'", "''") + "'"
    return f"{q}!{rng}" if rng else q


class SheetsClient:
    def __init__(self, spreadsheet_id: str, credentials=None, session=None, tries: int = 5,
                 sleep=time.sleep, timeout: float = 60):
        self.id = spreadsheet_id
        self.tries, self.sleep, self.timeout = tries, sleep, timeout
        self.calls = 0
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.service_account import Credentials
            check_key(credentials)
            creds = Credentials.from_service_account_file(os.path.expanduser(str(credentials)), scopes=SCOPES)
            session = AuthorizedSession(creds)
        self.session = session

    # ---- transport ------------------------------------------------------------------------
    def _call(self, method, path, **kw):
        url = f"{API}/{self.id}{path}"
        last = None
        for i in range(self.tries):
            self.calls += 1
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
            except Exception as e:   # requests' ConnectionError/Timeout, google.auth TransportError
                last = SheetsError(f"{method} {path or '/'}: {e!r}")
            else:
                if r.status_code < 300:
                    return r.json() if r.content else {}
                try:
                    msg = r.json().get("error", {}).get("message", r.text)
                except ValueError:
                    msg = r.text
                last = SheetsError(f"{method} {path or '/'}: HTTP {r.status_code}: {msg[:500]}", r.status_code)
                if r.status_code not in RETRY_STATUS:
                    raise last
            if i < self.tries - 1:
                self.sleep(min(64, 2 ** i) + random.random())
        raise last

    # ---- API ------------------------------------------------------------------------------
    def meta(self, fields: str | None = None) -> dict:
        return self._call("GET", "", params={"fields": fields} if fields else None)

    def batch_update(self, requests: list[dict]) -> list[dict]:
        if not requests:
            return []
        return self._call("POST", ":batchUpdate", json={"requests": requests}).get("replies", [])

    def values_get(self, ranges: list[str], render: str = "FORMATTED_VALUE") -> list[list[list]]:
        if not ranges:
            return []
        out = self._call("GET", "/values:batchGet",
                         params={"ranges": list(ranges), "valueRenderOption": render,
                                 "majorDimension": "ROWS"})
        return [vr.get("values", []) for vr in out.get("valueRanges", [])]

    def values_update(self, data: list[tuple[str, list[list]]], input: str = "USER_ENTERED") -> dict:
        if not data:
            return {}
        return self._call("POST", "/values:batchUpdate",
                          json={"valueInputOption": input,
                                "data": [{"range": r, "majorDimension": "ROWS", "values": v} for r, v in data]})
