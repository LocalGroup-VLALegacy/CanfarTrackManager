"""The state table (trackmanager/states.py): pure checks, no controller."""
import itertools
from pathlib import Path

import pytest
import yaml

from trackmanager import states as S

SPEC = Path(__file__).resolve().parent.parent / "trackmanager" / "templates" / "sheets" / "spec_v1.yaml"


def test_ten_stages_and_every_edge_target_is_known():
    assert len(S.STAGES) == 10 and set(S.TRANSITIONS) == set(S.STAGES)
    for frm, to, _ in S.edges():
        assert to in S.STAGES, (frm, to)


def test_every_stage_is_reachable_from_discovered():
    seen, todo = {S.DISCOVERED}, [S.DISCOVERED]
    while todo:
        for nxt in S.TRANSITIONS[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == set(S.STAGES)


def test_terminal_stages_have_no_out_edges():
    assert S.TERMINAL == {S.DONE, S.NEEDS_MANUAL, S.SKIPPED}
    assert all(not S.TRANSITIONS[s] for s in S.TERMINAL)
    assert all(S.TRANSITIONS[s] for s in set(S.STAGES) - S.TERMINAL)


def test_human_gates_and_settled():
    assert S.HUMAN_GATES == {S.AWAITING_DATA, S.AWAITING_REVIEW, S.AWAITING_DECISION}
    assert S.SETTLED == S.HUMAN_GATES | S.TERMINAL
    # a person acts on every edge out of a gate, the controller on the rest
    for gate in S.HUMAN_GATES:
        assert all(e.actor in (S.OPERATOR, S.REVIEWER) for e in S.TRANSITIONS[gate].values())


def test_every_listed_edge_passes_and_every_other_pair_raises():
    listed = {(f, t) for f, t, _ in S.edges()}
    for frm, to in itertools.product(S.STAGES, repeat=2):
        if (frm, to) in listed:
            assert S.check(frm, to) is S.TRANSITIONS[frm][to]
        else:
            with pytest.raises(S.IllegalTransition):
                S.check(frm, to)
    with pytest.raises(S.IllegalTransition):
        S.check("QUEUED", S.DONE)  # a pre-M1 name


def test_failable_stages_are_the_working_stages():
    assert S.FAILABLE == {S.DISCOVERED, S.STAGING, S.CALIBRATING, S.PROMOTING}
    # a retry target must be an edge out of AWAITING_DECISION
    for stage in S.FAILABLE - {S.DISCOVERED}:
        S.check(S.AWAITING_DECISION, stage)


def test_pending_edges_are_exactly_the_ones_without_a_handler_yet():
    """M3 wrote stage_local, so STAGING and its retry edge have handlers. Only the edges out of
    AWAITING_DATA (the pasted link and the skip) wait for M6."""
    pending = {(f, t): e.pending for f, t, e in S.edges() if e.pending}
    assert pending == {(S.AWAITING_DATA, S.STAGING): "M6", (S.AWAITING_DATA, S.SKIPPED): "M6"}
    for frm, to, e in S.edges():
        if S.DISCOVERED in (frm, to) or S.STAGING in (frm, to) and frm != S.AWAITING_DATA:
            assert not e.pending, (frm, to)


def test_activities():
    for stage in S.STAGES:
        for act in S.ACTIVITIES.get(stage, (None,)):
            S.check_activity(stage, act)
    with pytest.raises(S.IllegalTransition):
        S.check_activity(S.CALIBRATING, "waiting")
    with pytest.raises(S.IllegalTransition):
        S.check_activity(S.DONE, "queued")
    with pytest.raises(S.IllegalTransition):
        S.check_activity(S.CALIBRATING, None)
    assert set(S.WORKER_STAGE) <= set(S.ACTIVITIES)


def test_initial_stage_is_a_valid_activity():
    for spec in (None, {}, {"path": "/arc/x.tar"}):
        stage, act = S.initial_stage(None, spec)
        S.check_activity(stage, act)
    assert S.initial_stage(None, {"path": "/arc/x.tar"}) == (S.STAGING, S.QUEUED)   # has an input to stage
    assert S.initial_stage(None, {}) == (S.CALIBRATING, S.QUEUED)


def test_sheet_spec_colours_every_stage():
    colours = yaml.safe_load(SPEC.read_text())["stage_colors"]
    assert set(colours) == set(S.STAGES), set(colours) ^ set(S.STAGES)
