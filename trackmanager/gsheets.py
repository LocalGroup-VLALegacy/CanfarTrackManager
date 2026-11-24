"""Minimal Google Sheets API v4 client for the controller (plan §3.5, M4).

Four calls are all the bridge needs, and trackmanager/sheets.py already builds the raw v4 request
bodies, so this sits directly on google-auth's AuthorizedSession instead of gspread:

    c = SheetsClient(spreadsheet_id, credentials="/path/key.json")
    c.meta("sheets(properties,protectedRanges)")        spreadsheets.get with a fields mask
    c.batch_update([...requests...])                    spreadsheets.batchUpdate -> replies
    c.values_get(["'Tab'!A1:C9"])                       values:batchGet -> [[row], ...] per range
    c.values_update([("'Tab'!A2", [[1, 2]])])           values:batchUpdate (USER_ENTERED)

Scope is spreadsheets only for these; DriveClient (drive.file scope: only files this service
account created, plus the folder shared with it) creates each track's flag spreadsheet in the
project's flags folder. 429 and 5xx
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
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
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


def session_for(credentials, scopes=SCOPES):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials
    check_key(credentials)
    return AuthorizedSession(Credentials.from_service_account_file(os.path.expanduser(str(credentials)),
                                                                   scopes=scopes))


class _Client:
    base = API

    def __init__(self, ident: str, credentials=None, session=None, tries: int = 5,
                 sleep=time.sleep, timeout: float = 60, scopes=SCOPES):
        self.id = ident
        self.tries, self.sleep, self.timeout = tries, sleep, timeout
        self.calls = 0
        self.session = session if session is not None else session_for(credentials, scopes)

    # ---- transport ------------------------------------------------------------------------
    def _call(self, method, path, **kw):
        url = path if path.startswith("https://") else f"{self.base}/{self.id}{path}"
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

class SheetsClient(_Client):
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

    def copy_sheet_to(self, sheet_id: int, destination_id: str) -> dict:
        """Copy one tab (formulas, validation, formats; not protection) into another spreadsheet.
        Returns the new tab's properties (title "Copy of ...")."""
        return self._call("POST", f"/sheets/{sheet_id}:copyTo", json={"destinationSpreadsheetId": destination_id})


class DriveClient(_Client):
    """The project's flags folder: one spreadsheet per track, created and found by this client."""
    base = DRIVE

    def __init__(self, folder_id: str, credentials=None, session=None, **kw):
        super().__init__(folder_id, credentials, session, scopes=DRIVE_SCOPES, **kw)

    def folder(self) -> dict:
        return self._call("GET", "", params={"fields": "id,name,mimeType,trashed,capabilities(canAddChildren)",
                                             "supportsAllDrives": "true"})

    def create_spreadsheet(self, name: str, app_properties: dict) -> dict:
        return self._call("POST", DRIVE, params={"fields": "id,name,webViewLink", "supportsAllDrives": "true"},
                          json={"name": name, "mimeType": SHEET_MIME, "parents": [self.id],
                                "appProperties": {k: str(v) for k, v in app_properties.items()}})

    def find(self, app_properties: dict) -> list[dict]:
        """Spreadsheets in the folder carrying these appProperties (not trashed)."""
        q = [f"'{self.id}' in parents", "trashed = false", f"mimeType = '{SHEET_MIME}'"]
        q += [f"appProperties has {{ key='{k}' and value='{v}' }}" for k, v in app_properties.items()]
        out = self._call("GET", DRIVE, params={"q": " and ".join(q), "fields": "files(id,name,webViewLink)",
                                               "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
        return out.get("files", [])

    def get(self, file_id: str) -> dict:
        return self._call("GET", f"{DRIVE}/{file_id}", params={"fields": "id,name,trashed,parents",
                                                               "supportsAllDrives": "true"})
