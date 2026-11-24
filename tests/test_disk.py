"""Disk gate model (trackmanager/disk.py): footprints by phase, admission, in-cycle reservation."""
import pytest

from trackmanager import disk
from trackmanager import states as S
from trackmanager.config import Limits

F = 8.0


def st(eb, stage, activity=None, size=100, **kw):
    return {"eb": eb, "stage": stage, "activity": activity, "est_size_gb": size, "iteration": 1, **kw}


def lim(quota=None, free=0, factor=F):
    return Limits({"calibrate": 9, "stage": 9}, None, free, quota, factor)


@pytest.mark.parametrize("state, held", [
    (st("a", S.STAGING, S.QUEUED), 0),
    (st("a", S.STAGING, S.RUNNING), 100),
    (st("a", S.CALIBRATING, S.QUEUED), 100),
    (st("a", S.CALIBRATING, S.RUNNING), 800),
    (st("a", S.CALIBRATING, S.PUBLISHING), 800),
    (st("a", S.CALIBRATING, S.RUNNING, iteration=3), 800 + 100 * 2 * disk.PRODUCTS_FACTOR),
    (st("a", S.AWAITING_REVIEW), 100 * (1 + disk.SPLIT_FACTOR + disk.PRODUCTS_FACTOR)),
    (st("a", S.PROMOTING, iteration=2), 100 * (1 + disk.SPLIT_FACTOR + 2 * disk.PRODUCTS_FACTOR)),
    (st("a", S.DONE, completed_iteration=2), 100 * (disk.SPLIT_FACTOR + 2 * disk.PRODUCTS_FACTOR)),
    (st("a", S.AWAITING_DECISION, reason="failed", failed_from=S.CALIBRATING, failed_activity=S.RUNNING), 800),
    (st("a", S.AWAITING_DECISION, reason="failed", failed_from=S.STAGING, failed_activity=S.QUEUED), 0),
    (st("a", S.AWAITING_DECISION, reason="skip_candidate"), 0),
    (st("a", S.AWAITING_DATA), 0),
    (st("a", S.SKIPPED), 0),
])
def test_what_a_track_holds_by_phase(state, held):
    assert disk.held_gb(state, disk.size_gb(state), F) == pytest.approx(held)


def test_size_prefers_the_staged_size_then_the_estimate_then_the_config():
    assert disk.size_gb({"sdm_bytes": 5e9, "est_size_gb": 9}, {"size_gb": 1}) == 5
    assert disk.size_gb({"est_size_gb": 9}, {"size_gb": 1}) == 9
    assert disk.size_gb({}, {"size_gb": 1}) == 1
    assert disk.size_gb({}, {}) is None
    assert disk.held_gb(st("a", S.CALIBRATING, S.RUNNING, size=None), None, F) == 0


def test_gate_is_off_without_a_quota():
    g = disk.Gate([st("a", S.CALIBRATING, S.QUEUED, size=1e6)], lim(quota=None), lambda s: {})
    assert g.admit(st("b", S.CALIBRATING, S.QUEUED, size=1e6)) is None
    assert g.summary()["quota_tb"] is None


def test_a_queued_track_is_admitted_only_if_its_growth_fits():
    running = st("r", S.CALIBRATING, S.RUNNING)                       # holds 800 GB
    g = disk.Gate([running], lim(quota=1.5, free=0.2), lambda s: {})  # 1500 - 200 - 800 = 500 free
    # staging a 100 GB track needs 100: fits; calibrating it later needs 700 more
    assert g.admit(st("s", S.STAGING, S.QUEUED)) is None
    assert g.free() == pytest.approx(400)
    why = g.admit(st("c", S.CALIBRATING, S.QUEUED))                    # holds 100 now, 800 started: needs 700
    assert why and why.startswith("disk: needs 0.70 TB, 0.40 TB free")
    assert "quota 1.5 TB" in why


def test_reservations_within_a_cycle_count_and_a_blocked_track_does_not_block_a_smaller_one():
    g = disk.Gate([], lim(quota=1.0), lambda s: {})                    # 1000 GB
    assert g.admit(st("a", S.CALIBRATING, S.QUEUED, size=100)) is None   # needs 700
    assert g.admit(st("b", S.CALIBRATING, S.QUEUED, size=100)) is not None   # 700 > 300 left
    assert g.admit(st("c", S.CALIBRATING, S.QUEUED, size=30)) is None        # 210 fits
    assert g.used == pytest.approx(910) and g.free() == pytest.approx(90)


def test_a_track_that_can_never_fit_says_so():
    g = disk.Gate([], lim(quota=0.5), lambda s: {})
    why = g.admit(st("big", S.CALIBRATING, S.QUEUED, size=500))        # 500 * 8 = 4 TB
    assert "needs 3.50 TB, 0.50 TB free" in why


def test_unsized_tracks_hold_nothing_and_are_counted():
    unsized = st("u", S.CALIBRATING, S.QUEUED, size=None)
    g = disk.Gate([unsized], lim(quota=0.1), lambda s: {})
    assert g.admit(unsized) is None and g.summary()["unsized_tracks"] == 1


def test_config_size_gb_is_used_when_there_is_no_estimate():
    s = st("c", S.CALIBRATING, S.QUEUED, size=None)
    g = disk.Gate([s], lim(quota=1.0), lambda s: {"size_gb": 100})
    assert g.admit(s) is None and g.used == pytest.approx(800)         # 100 held + 700 reserved


def test_summary_is_in_tb():
    g = disk.Gate([st("r", S.CALIBRATING, S.RUNNING)], lim(quota=2, free=0.5), lambda s: {})
    assert g.summary() == {"quota_tb": 2.0, "min_free_tb": 0.5, "modelled_used_tb": 0.8, "free_tb": 0.7,
                           "disk_factor": 8.0, "unsized_tracks": 0}


def test_a_reopened_track_still_holds_its_superseded_splits_and_earlier_products():
    from trackmanager import disk
    base = {"eb": "r", "stage": "STAGING", "activity": "queued", "iteration": 3}
    fresh = disk.held_gb({**base, "iteration": 1}, 100.0, 8.0)
    again = disk.held_gb({**base, "reopened": [{"from_iteration": 2}]}, 100.0, 8.0)
    assert fresh == 0.0
    assert again == pytest.approx(100 * (disk.SPLIT_FACTOR + 2 * disk.PRODUCTS_FACTOR))
    done = disk.held_gb({**base, "stage": "DONE", "completed_iteration": 3, "reopened": [{}]}, 100.0, 8.0)
    assert done == pytest.approx(100 * (2 * disk.SPLIT_FACTOR + 3 * disk.PRODUCTS_FACTOR))
