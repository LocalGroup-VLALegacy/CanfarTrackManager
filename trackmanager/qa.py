"""QA products for reviewers (plan §5): where they live, how a reviewer reaches them, who may
read them, and the index pages. Every function takes the controller (`ctl`) for its config,
database paths and event log."""
from __future__ import annotations

import html
import os
import subprocess
from pathlib import Path

from .store import read_json


def page(title, body):
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title><style>body{{font-family:sans-serif;margin:2em}}"
            "td,th{padding:4px 10px;text-align:left;border-bottom:1px solid #ddd}</style></head>"
            f"<body><h1>{html.escape(title)}</h1>{body}</body></html>\n")


def write_if_changed(path, text):
    path = Path(path)
    if path.exists() and path.read_text() == text:
        return
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class QaAccessError(OSError):
    """setfacl could not give qa.group read access."""


def qa_dir(ctl, eb, n):
    """<products_dir>/<EB>/iter_<n>/: where the worker writes this iteration's products."""
    return ctl.out_dir("products_dir", eb) / f"iter_{n}"


def qa_url(ctl, path):
    """Browser link through the ARC files service, or None for a path outside /arc."""
    path = str(Path(path).resolve())
    if not path.startswith("/arc/"):
        return None
    return ctl.qa("url_base").rstrip("/") + path[len("/arc"):]


def grant_qa_read(ctl, path, recursive=True):
    """Give qa.group read (and directory traverse) on a QA folder, as Storage Management's
    "Read" does: a POSIX ACL entry, plus a default entry so files added later inherit it.
    Raises QaAccessError if setfacl fails."""
    group = ctl.qa("group")
    if not group or not Path(path).exists():
        return True
    cmds = [["setfacl", *(["-R"] if recursive else []), "-m", f"g:{group}:rX", str(path)]]
    if Path(path).is_dir():
        cmds.append(["setfacl", *(["-R"] if recursive else []), "-d", "-m", f"g:{group}:rX", str(path)])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            r = None
            err = repr(e)
        if r is None or r.returncode != 0:
            err = r.stderr.strip() if r is not None else err
            raise QaAccessError(f"{' '.join(cmd)}: {err}")
    return True


def write_index(ctl, states):
    """<products_dir>/index.html (every track) and <EB>/index.html (every iteration), the
    "QA home" and "all iterations" pages reviewers reach from the sheet and the QA pages."""
    base = ctl.base_dir("products_dir")
    if not base.is_dir():
        return
    rows = []
    for eb, st in sorted(states.items()):
        tdir = base / eb
        iters = sorted((int(d.name[5:]) for d in tdir.glob("iter_*")
                        if d.name[5:].isdigit() and (d / "index.html").exists()), reverse=True) \
            if tdir.is_dir() else []
        if not iters:
            rows.append((eb, st, None))
            continue
        approved = (read_json(tdir / "approved.json") or {}).get("iteration")
        items = []
        for n in iters:
            verdict = (read_json(ctl.r.review(eb, n)) or {}).get("verdict", "")
            tag = "approved" if n == approved else verdict.replace("_", " ")
            items.append(f'<li><a href="iter_{n}/index.html">iteration {n}</a>'
                         + (f" ({html.escape(tag)})" if tag else "") + "</li>")
        write_if_changed(tdir / "index.html", page(
            f"{eb}: QA by iteration",
            f'<p><a href="../index.html">QA home (all tracks)</a></p>'
            f"<p>Stage: {html.escape(st['stage'])}, current iteration {st.get('iteration', 1)}</p>"
            f"<ul>{''.join(items)}</ul>"))
        rows.append((eb, st, iters[0]))
    body = ["<table><tr><th>Track</th><th>Stage</th><th>Iteration</th><th>Latest QA</th>"
            "<th>All iterations</th></tr>"]
    for eb, st, latest in rows:
        body.append(f"<tr><td>{html.escape(eb)}</td><td>{html.escape(st['stage'])}</td>"
                    f"<td>{st.get('iteration', 1)}</td>"
                    + (f'<td><a href="{eb}/iter_{latest}/index.html">iteration {latest}</a></td>'
                       f'<td><a href="{eb}/index.html">all</a></td>' if latest else "<td></td><td></td>")
                    + "</tr>")
    body.append("</table>")
    write_if_changed(base / "index.html", page(f"{ctl.cfg.name or ctl.r.path.name}: QA home", "".join(body)))
