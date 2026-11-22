"""The track state machine as data (plan §3.3, consolidated to 10 stages in M1).

A stage says who acts next and what resource the track holds. Progress inside a stage is a
field of state/<EB>.json (`activity`, `request_state`, `reason`, `hold`), and steps that finish
within one cycle (preflight, a verdict being applied, a failure being recorded) are the *reason*
on an event, not stages.

    DISCOVERED -> AWAITING_DATA -> STAGING -> CALIBRATING -> AWAITING_REVIEW -> PROMOTING -> DONE
                                                 ^______________| (RESTART: iteration + 1)
    any working stage -> AWAITING_DECISION -> retry | include | skip | needs_manual

`Controller.set_stage` calls `check()`, so no code path can make an edge this table lacks.
`Edge.pending` names the milestone whose handler is not written yet; those edges are covered
by tests/test_states.py only.
"""
from __future__ import annotations

from dataclasses import dataclass

DISCOVERED = "DISCOVERED"
AWAITING_DATA = "AWAITING_DATA"
STAGING = "STAGING"
CALIBRATING = "CALIBRATING"
AWAITING_REVIEW = "AWAITING_REVIEW"
PROMOTING = "PROMOTING"
DONE = "DONE"
AWAITING_DECISION = "AWAITING_DECISION"
NEEDS_MANUAL = "NEEDS_MANUAL"
SKIPPED = "SKIPPED"

STAGES = (DISCOVERED, AWAITING_DATA, STAGING, CALIBRATING, AWAITING_REVIEW, PROMOTING, DONE,
          AWAITING_DECISION, NEEDS_MANUAL, SKIPPED)

# Progress inside a stage. Stages not listed here have no `activity`.
QUEUED, RUNNING, PUBLISHING = "queued", "running", "publishing"
ACTIVITIES = {STAGING: (QUEUED, RUNNING), CALIBRATING: (QUEUED, RUNNING, PUBLISHING)}

# Worker stages: the kind of session a queued track waits for (config: controller.max_workers).
WORKER_STAGE = {STAGING: "stage", CALIBRATING: "calibrate"}
WORKER_KINDS = tuple(WORKER_STAGE.values())

# Request progress inside AWAITING_DATA, and the reasons carried by AWAITING_DECISION.
REQUEST_STATES = ("needs_request", "requested")
REASONS = ("skip_candidate", "preflight_failed", "failed")

CONTROLLER, OPERATOR, REVIEWER = "controller", "operator", "reviewer"

# Reviewer verdicts (flag-tab M1): COMPLETE -> pass, RESTART -> restart; MANUAL REVIEW and
# HELP REQUESTED hold the track for the operator (no rerun) until a later pass or restart.
REVIEW_ACTIONS = ("pass", "restart", "manual_review", "help_requested")
INPUT_FILES = ("manual_flagging.txt", "refant_ignore.txt")  # what a restart may carry


@dataclass(frozen=True)
class Edge:
    actor: str
    note: str = ""
    pending: str | None = None  # milestone that writes the handler (M3, M6)


def _e(actor, note="", pending=None):
    return Edge(actor, note, pending)


TRANSITIONS: dict[str, dict[str, Edge]] = {
    DISCOVERED: {
        AWAITING_DATA: _e(CONTROLLER, "preflight ok, source nrao_link"),
        STAGING: _e(CONTROLLER, "preflight ok, source arc_path"),
        AWAITING_DECISION: _e(CONTROLLER, "reason skip_candidate or preflight_failed"),
    },
    AWAITING_DATA: {
        STAGING: _e(OPERATOR, "link pasted into the dashboard", "M6"),
        SKIPPED: _e(OPERATOR, "operator skips the request", "M6"),
    },
    STAGING: {
        CALIBRATING: _e(CONTROLLER, "raw data verified"),
        AWAITING_DECISION: _e(CONTROLLER, "reason failed"),
    },
    CALIBRATING: {
        AWAITING_REVIEW: _e(CONTROLLER, "run verified, QA published"),
        PROMOTING: _e(CONTROLLER, "run verified; only when review.enabled is false"),
        AWAITING_DECISION: _e(CONTROLLER, "reason failed"),
    },
    AWAITING_REVIEW: {
        PROMOTING: _e(REVIEWER, "verdict COMPLETE"),
        CALIBRATING: _e(REVIEWER, "verdict RESTART: iteration + 1, queued"),
    },
    PROMOTING: {
        DONE: _e(CONTROLLER, "splits promoted, raw deleted"),
        AWAITING_DECISION: _e(CONTROLLER, "reason failed"),
    },
    AWAITING_DECISION: {
        # `retry` returns to st["failed_from"]; the target is checked against this row.
        STAGING: _e(OPERATOR, "retry a failed staging"),
        CALIBRATING: _e(OPERATOR, "retry a failed calibration"),
        PROMOTING: _e(OPERATOR, "retry a failed promotion"),
        DISCOVERED: _e(OPERATOR, "include a skip candidate, or recheck preflight after a cfg fix"),
        SKIPPED: _e(OPERATOR, "skip or abort"),
        NEEDS_MANUAL: _e(OPERATOR, "something outside the workflow has to happen"),
    },
    DONE: {},
    NEEDS_MANUAL: {},
    SKIPPED: {},
}

# Stages where a person has to act (the dashboard's "needs you" queue).
HUMAN_GATES = frozenset({AWAITING_DATA, AWAITING_REVIEW, AWAITING_DECISION})
TERMINAL = frozenset({DONE, NEEDS_MANUAL, SKIPPED})
# Stages in which the controller has no work to do until something outside it changes.
SETTLED = HUMAN_GATES | TERMINAL
# Stages from which a failure is recorded as AWAITING_DECISION (reason failed, failed_from).
FAILABLE = frozenset(s for s, row in TRANSITIONS.items() if AWAITING_DECISION in row)


class IllegalTransition(ValueError):
    pass


def check(frm: str, to: str) -> Edge:
    """The Edge for frm -> to; IllegalTransition if the table has no such edge."""
    if frm not in TRANSITIONS:
        raise IllegalTransition(f"unknown stage {frm!r}")
    edge = TRANSITIONS[frm].get(to)
    if edge is None:
        raise IllegalTransition(f"no transition {frm} -> {to}")
    return edge


def edges():
    """Every (from, to, Edge) in the table."""
    return [(f, t, e) for f, row in TRANSITIONS.items() for t, e in row.items()]


def check_activity(stage: str, activity: str | None) -> None:
    allowed = ACTIVITIES.get(stage)
    if allowed is None:
        if activity is not None:
            raise IllegalTransition(f"{stage} has no activity (got {activity!r})")
    elif activity not in allowed:
        raise IllegalTransition(f"{stage} activity {activity!r} not in {allowed}")


def initial_stage(cfg=None, spec=None) -> tuple[str, str | None]:
    """Where a track from the config (`tracks:`) or an operator `request` enters: STAGING if it
    names an input (`path`), else CALIBRATING (the placeholder worker, or raw data already in
    place). Tracks found by the archive sync start at DISCOVERED instead (stages/discover.py)."""
    if (spec or {}).get("path"):
        return STAGING, QUEUED
    return CALIBRATING, QUEUED
