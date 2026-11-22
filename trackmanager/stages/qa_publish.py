"""CALIBRATING / publishing: after a verified run, give reviewers the flag inputs, group read
access and the link (plan §5)."""
from __future__ import annotations
import shutil

from .. import qa
from ..states import INPUT_FILES


def run(ctl, st):
    """After a verified run: the reviewers' copy of the flag inputs, group read, the link."""
    eb, n = st["eb"], st.get("iteration", 1)
    qd = qa.qa_dir(ctl, eb, n)
    if not qd.is_dir():
        return
    dst = qd / "inputs"
    for name in INPUT_FILES:
        if (ctl.r.inputs(eb, n) / name).exists():
            dst.mkdir(exist_ok=True)
            shutil.copy2(ctl.r.inputs(eb, n) / name, dst / name)
    ctl.base_dir("products_dir").mkdir(parents=True, exist_ok=True)
    qa.grant_qa_read(ctl, ctl.base_dir("products_dir"), recursive=False)
    qa.grant_qa_read(ctl, qd.parent, recursive=False)
    qa.grant_qa_read(ctl, qd)
    st["qa_url"] = qa.qa_url(ctl, qd / "index.html")
