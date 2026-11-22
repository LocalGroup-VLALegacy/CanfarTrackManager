"""CALIBRATING / publishing: after a verified run, give reviewers the flag inputs, group read
access and the link (plan §5), then hand the track to its reviewer (or straight to promotion
when review is off). A failure keeps the track here and tries again next cycle; after
PUBLISH_RETRIES failed cycles in a row it goes to AWAITING_DECISION."""
from __future__ import annotations

import shutil

from .. import qa
from .. import states as S
from ..store import read_json, write_json
from . import promote

PUBLISH_RETRIES = 5


def _publish(ctl, st, qd):
    eb, n = st["eb"], st.get("iteration", 1)
    dst = qd / "inputs"
    for name in S.INPUT_FILES:
        if (ctl.r.inputs(eb, n) / name).exists():
            dst.mkdir(exist_ok=True)
            shutil.copy2(ctl.r.inputs(eb, n) / name, dst / name)
    ctl.base_dir("products_dir").mkdir(parents=True, exist_ok=True)
    qa.grant_qa_read(ctl, ctl.base_dir("products_dir"), recursive=False)
    qa.grant_qa_read(ctl, qd.parent, recursive=False)
    qa.grant_qa_read(ctl, qd)
    st["qa_url"] = qa.qa_url(ctl, qd / "index.html")


def run(ctl, st):
    eb, n, k = st["eb"], st.get("iteration", 1), st.get("attempt") or 0
    qd = qa.qa_dir(ctl, eb, n)
    try:
        if qd.is_dir():  # a placeholder worker writes no QA products
            _publish(ctl, st, qd)
    except qa.QaAccessError as e:
        failures = st.get("publish_failures", 0) + 1
        if str(e) != st.get("publish_error"):  # one event per distinct error
            ctl.event(eb, S.CALIBRATING, S.CALIBRATING, f"publishing iteration {n} failed: {e}",
                      from_activity=S.PUBLISHING, activity=S.PUBLISHING)
        st.update(publish_failures=failures, publish_error=str(e))
        if failures >= PUBLISH_RETRIES:
            ctl.fail(st, f"publishing iteration {n} failed {failures} times: {e}")
        else:
            write_json(ctl.r.state(eb), st)
        return
    for key in ("publish_failures", "publish_error"):
        st.pop(key, None)
    att = read_json(ctl.r.attempt(eb, n, k)) or {}
    to = S.AWAITING_REVIEW if ctl.cfg.review.enabled else S.PROMOTING
    ctl.set_stage(st, to, None, f"iteration {n} attempt {k} verified "
                                f"(platform: {att.get('platform_status')})")
    if to == S.PROMOTING:
        promote.run(ctl, st)
