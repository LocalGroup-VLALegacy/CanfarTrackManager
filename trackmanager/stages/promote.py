"""PROMOTING: move the approved iteration out of the track database (plan §3.2 Retention)."""
from __future__ import annotations
import shutil
from pathlib import Path

from .. import qa
from ..store import iso, read_json, write_json
from .. import retention
from .. import states as S


def run(ctl, st):
    """PROMOTING -> DONE: move the approved iteration's splits to science_dir/<EB>/ and its
    products to products_dir/<EB>/, write provenance.json, then delete raw/ (unless
    keep_raw_after_done). Every step is a rename or a check, so a controller killed part way
    repeats it safely on the next cycle. Conflicts go to AWAITING_DECISION."""
    eb, n = st["eb"], st.get("iteration", 1)
    idir, res = ctl.r.iter_dir(eb, n), read_json(ctl.r.result(eb, n)) or {}
    sci, prod = ctl.out_dir("science_dir", eb), Path(res.get("qa_dir") or qa.qa_dir(ctl, eb, n))
    try:
        sci.mkdir(parents=True, exist_ok=True)
        moved = []
        for src in sorted((idir / "splits").glob("*")) if (idir / "splits").is_dir() else []:
            if (sci / src.name).exists():
                raise FileExistsError(f"{sci / src.name} already exists")
            shutil.move(str(src), str(sci / src.name))
            moved.append(src.name)
        if (idir / "splits").is_dir() and not any((idir / "splits").iterdir()):
            (idir / "splits").rmdir()
        splits = sorted(p.name for p in sci.iterdir() if p.name != "provenance.json")
        if not splits:
            raise FileNotFoundError(f"no splits in {idir / 'splits'} or {sci}")
        if not prod.is_dir():
            raise FileNotFoundError(f"no QA products in {prod}")
        review = read_json(ctl.r.review(eb, n)) or {}
        prov = {"eb": eb, "iteration": n, "sdm": res.get("sdm"), "splits": splits,
                "products": str(prod), "qa_url": qa.qa_url(ctl, prod / "index.html"),
                "inputs_sha256": res.get("inputs", {}),
                "inputs_dir": str(ctl.r.inputs(eb, n)), "reviewer": review.get("by"),
                "verdict_at": review.get("decided") or review.get("applied"),
                "worker_session": res.get("session"), "qaplotter": res.get("qaplotter"),
                "track_db": str(ctl.r.track(eb)), "promoted": iso(), "controller": ctl.me}
        write_json(sci / "provenance.json", prov)
        write_json(prod / "provenance.json", prov)
        write_json(prod.parent / "approved.json", prov)  # marks the approved iteration
    except OSError as e:
        ctl.fail(st, f"promotion of iteration {n} failed: {e}")
        return
    if moved:
        ctl.event(eb, "retention", "promoted", f"iter_{n}: {len(moved)} split(s) -> {sci}; "
                   f"approved QA {prod}")
    retention.trim_work(ctl, eb, n)
    raw = ctl.r.track(eb) / "raw"
    if not ctl.retention("keep_raw_after_done") and (raw.exists() or raw.is_symlink()):
        if raw.is_symlink():
            raw.unlink()  # never follow a link into the operator's own data
        else:
            shutil.rmtree(raw, ignore_errors=True)  # rmtree does not follow links inside raw/
        ctl.event(eb, "retention", "raw_deleted", "raw data deleted after the track passed")
    raw_gone = not (raw.exists() or raw.is_symlink())
    st.update(science_dir=str(sci), products_dir=str(prod), completed_iteration=n,
              completed_attempt=st.get("attempt"), completed_by=prov["reviewer"],
              completed_at=st.get("completed_at") or iso(),
              raw_deleted_at=(st.get("raw_deleted_at") or iso()) if raw_gone else None)
    k = st.get("attempt") or 0
    att = read_json(ctl.r.attempt(eb, n, k)) if k else None
    if att:
        ctl.job_row(eb, n, k, att, promoted=st["completed_at"])
    ctl.set_stage(st, S.DONE, None, f"iteration {n} promoted to {sci}")
