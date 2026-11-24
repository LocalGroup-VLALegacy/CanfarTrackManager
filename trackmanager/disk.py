"""Disk gate (plan §3.3 item 7): how much of the project's space each track holds or will need.

`controller.quota_tb` is the allocation (CANFAR gives no way to read it, so it is configured);
`min_free_tb` of it is always kept unreserved. The footprint of a track is modelled from the size
of its SDM, S, by what phase it is in (plan §3.2 quota model, IC10 ratios):

    STAGING, running                 S               raw data arriving
    CALIBRATING, queued              S               raw staged, workdir not yet made
    CALIBRATING, running/publishing  disk_factor x S raw + workdir (+ earlier iterations' products)
    AWAITING_REVIEW, PROMOTING       (1 + SPLIT + PRODUCTS x iteration) x S   raw + splits + products
    DONE                             (SPLIT + PRODUCTS x iteration) x S       splits + products
    AWAITING_DECISION                the phase it failed in (a failed run keeps its workdir)
    anything else                    0

A track that is queued is admitted if what it would hold once started, minus what it holds now,
fits in what is left. Nothing is persisted: the figure is derived from state each cycle, so a
restarted controller agrees with the one it replaced. Reservations made earlier in the same
cycle count (the admitted tracks only change state when they are submitted). A track whose size
is unknown (no manifest, TAP estimate or `size_gb`) holds nothing and is always admitted; the
dashboard counts such tracks.

Raw data reached through a symlink (arc_path) is counted as if it were copied: the input may or
may not count against the same allocation, and the model errs on the side of keeping room.
"""
from __future__ import annotations

from . import states as S

SPLIT_FACTOR = 0.83        # kept science splits, as a multiple of the SDM size
PRODUCTS_FACTOR = 0.056    # QA products per iteration


def size_gb(st: dict, spec: dict | None = None) -> float | None:
    """S: the staged SDM's real size, else the TAP estimate, else the config's `size_gb`."""
    if st.get("sdm_bytes"):
        return st["sdm_bytes"] / 1e9
    return st.get("est_size_gb") or (spec or {}).get("size_gb") or None


def _phase(st: dict) -> tuple[str, str | None]:
    """(stage, activity) the track is in, looking through AWAITING_DECISION to where it failed."""
    if st["stage"] == S.AWAITING_DECISION and st.get("reason") == "failed":
        return st.get("failed_from") or "", st.get("failed_activity")
    return st["stage"], st.get("activity")


def held_gb(st: dict, size: float | None, factor: float, started: bool = False) -> float:
    """GB the track holds now; with started=True, what it will hold once its queued activity runs."""
    if not size:
        return 0.0
    stage, activity = _phase(st)
    n = st.get("iteration", 1) or 1
    earlier = PRODUCTS_FACTOR * max(n - 1, 0)
    # a reopened track (reopen.py) keeps each earlier promotion's splits aside (<EB>.superseded-*)
    kept = size * SPLIT_FACTOR * len(st.get("reopened") or [])
    if stage == S.STAGING:
        return (size if (activity == S.RUNNING or started) else 0.0) + size * earlier + kept
    if stage == S.CALIBRATING:
        if activity in (S.RUNNING, S.PUBLISHING) or started:
            return size * (factor + earlier) + kept
        return size * (1 + earlier) + kept
    if stage in (S.AWAITING_REVIEW, S.PROMOTING):
        return size * (1 + SPLIT_FACTOR + PRODUCTS_FACTOR * n) + kept
    if stage == S.DONE:
        n = st.get("completed_iteration") or n
        return size * (SPLIT_FACTOR + PRODUCTS_FACTOR * n) + kept
    return 0.0


class Gate:
    """One cycle's disk budget. `gate.admit` is the scheduler's admit() hook."""

    def __init__(self, states, limits, spec_of=lambda st: {}):
        self.quota = limits.quota_tb * 1e3 if limits.quota_tb else None
        self.min_free = (limits.min_free_tb or 0) * 1e3
        self.factor = limits.disk_factor
        self.spec_of = spec_of
        self.used = 0.0
        self.unsized = 0
        for st in states:
            size = size_gb(st, spec_of(st))
            self.used += held_gb(st, size, self.factor)
            self.unsized += size is None and st["stage"] in (S.STAGING, S.CALIBRATING)

    def free(self) -> float | None:
        return None if self.quota is None else self.quota - self.min_free - self.used

    def admit(self, st: dict) -> str | None:
        if self.quota is None:
            return None
        size = size_gb(st, self.spec_of(st))
        need = held_gb(st, size, self.factor, started=True) - held_gb(st, size, self.factor)
        free = self.free()
        if need > free:
            return (f"disk: needs {need / 1e3:.2f} TB, {max(free, 0) / 1e3:.2f} TB free "
                    f"(quota {self.quota / 1e3:g} TB, keep {self.min_free / 1e3:g} TB, "
                    f"{self.used / 1e3:.2f} TB held)")
        self.used += need
        return None

    def summary(self) -> dict:
        tb = lambda gb: None if gb is None else round(gb / 1e3, 3)
        return {"quota_tb": tb(self.quota), "min_free_tb": tb(self.min_free),
                "modelled_used_tb": tb(self.used), "free_tb": tb(self.free()),
                "disk_factor": self.factor, "unsized_tracks": self.unsized}
