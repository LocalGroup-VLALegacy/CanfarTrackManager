"""Scheduling rules (trackmanager/scheduler.py), pure."""
from trackmanager import states as S
from trackmanager.config import Limits
from trackmanager.scheduler import plan


def tr(eb, stage=S.CALIBRATING, activity=S.QUEUED, queued_at="2026-01-01T00:00:00Z", **kw):
    return {"eb": eb, "stage": stage, "activity": activity, "queued_at": queued_at, **kw}


def lim(calibrate=1, stage=None, total=None):
    mw = {"calibrate": calibrate}
    if stage is not None:
        mw["stage"] = stage
    return Limits(mw, total, None)


def ebs(items):
    return [st["eb"] for st in items]


def test_order_is_priority_then_queue_time_then_name():
    states = [tr("late", queued_at="2026-01-03T00:00:00Z"), tr("early", queued_at="2026-01-01T00:00:00Z"),
              tr("vip", queued_at="2026-01-09T00:00:00Z"), tr("b"), tr("a")]
    prio = {"vip": 5}
    p = plan(states, lim(10), priority=lambda st: prio.get(st["eb"], 0))
    # a, b and early were queued at the same time, so the name breaks the tie
    assert ebs(p.submit) == ["vip", "a", "b", "early", "late"]


def test_limit_leaves_the_rest_queued_with_positions_and_reason():
    states = [tr("a"), tr("b", queued_at="2026-01-02T00:00:00Z"), tr("c", queued_at="2026-01-03T00:00:00Z")]
    p = plan(states, lim(1))
    assert ebs(p.submit) == ["a"]
    assert p.waiting == {"b": {"position": 1, "blocked_by": "max_workers.calibrate (1/1)"},
                         "c": {"position": 2, "blocked_by": "max_workers.calibrate (1/1)"}}


def test_running_tracks_and_orphans_take_slots():
    states = [tr("run", activity=S.RUNNING), tr("q")]
    assert ebs(plan(states, lim(2)).submit) == ["q"]
    assert plan(states, lim(2), orphans={"calibrate": 1}).submit == []
    p = plan(states, lim(3), orphans={"calibrate": 1})
    assert ebs(p.submit) == ["q"] and p.in_use["calibrate"] == 3


def test_publishing_and_other_stages_take_no_slot():
    states = [tr("pub", activity=S.PUBLISHING), tr("rev", stage=S.AWAITING_REVIEW, activity=None), tr("q")]
    assert ebs(plan(states, lim(1)).submit) == ["q"]


def test_zero_holds_a_stage_and_raising_the_limit_releases_it():
    states = [tr("a"), tr("b")]
    p = plan(states, lim(0))
    assert p.submit == [] and p.waiting["a"]["blocked_by"] == "max_workers.calibrate (0/0)"
    assert ebs(plan(states, lim(2)).submit) == ["a", "b"]   # next cycle, limit raised


def test_a_kind_without_a_limit_is_held_not_unlimited():
    p = plan([tr("s", stage=S.STAGING)], lim(5))
    assert p.submit == [] and p.waiting["s"]["blocked_by"] == "max_workers.stage is not set"


def test_stages_have_separate_caps_and_one_shared_cap():
    states = [tr("c1"), tr("c2"), tr("s1", stage=S.STAGING), tr("s2", stage=S.STAGING)]
    p = plan(states, lim(1, stage=1))
    assert sorted(ebs(p.submit)) == ["c1", "s1"]
    p = plan(states, lim(5, stage=5, total=3))
    assert len(p.submit) == 3 and "max_sessions (3/3)" in list(p.waiting.values())[0]["blocked_by"]
    # running sessions of either kind count against the shared cap
    p = plan([tr("r", stage=S.STAGING, activity=S.RUNNING), tr("c1")], lim(5, stage=5, total=1))
    assert p.submit == [] and p.total_in_use == 1


def test_hold_flag_keeps_a_track_out_of_the_queue_without_blocking_others():
    p = plan([tr("held", hold=True), tr("go")], lim(1))
    assert ebs(p.submit) == ["go"] and p.held == 1 and "held" not in p.waiting


def test_shared_admit_gate_leaves_a_track_queued_with_its_reason():
    def admit(st):
        return "disk: 4.1 TB needed, 3.0 TB free" if st["eb"] == "big" else None
    p = plan([tr("big"), tr("small", queued_at="2026-01-02T00:00:00Z")], lim(5), admit=admit)
    assert ebs(p.submit) == ["small"]            # the big track does not block the small one
    assert p.waiting["big"] == {"position": 1, "blocked_by": "disk: 4.1 TB needed, 3.0 TB free"}


def test_stage_and_calibrate_sessions_share_max_sessions_but_not_per_kind_slots():
    states = [tr("s1", S.STAGING), tr("s2", S.STAGING), tr("c1"), tr("c2", queued_at="2026-01-02T00:00:00Z"),
              tr("r", S.CALIBRATING, S.RUNNING)]
    p = plan(states, lim(calibrate=2, stage=2, total=3))
    # r runs (1 of 3 sessions). Queue order is c1, s1, s2, c2: c1 and s1 fit; s2 hits the shared
    # cap; c2 hits the calibrate cap (r + c1) before the shared one is looked at.
    assert ebs(p.submit) == ["c1", "s1"]
    assert p.waiting == {"s2": {"position": 1, "blocked_by": "max_sessions (3/3)"},
                         "c2": {"position": 2, "blocked_by": "max_workers.calibrate (2/2)"}}
    assert p.in_use == {"stage": 1, "calibrate": 2} and p.total_in_use == 3


def test_orphan_stage_sessions_take_stage_slots_only():
    p = plan([tr("s1", S.STAGING), tr("c1")], lim(calibrate=1, stage=1), orphans={"stage": 1})
    assert ebs(p.submit) == ["c1"] and p.waiting["s1"]["blocked_by"] == "max_workers.stage (1/1)"
