"""Scheduling: which queued tracks get a worker session this cycle (plan §3.3 item 7).

Pure functions over the track states, so the rules are testable without a controller. A track
is *queued* when its stage has a worker kind (states.WORKER_STAGE) and `activity == "queued"`.
Tracks that don't pass a gate stay queued in the database with `queue_position` and
`blocked_by`; the next cycle re-evaluates them, so raising a limit (or a slot freeing up)
releases them within one poll.

Gates, in order, per track:
  1. `limits.max_workers[kind]`   live sessions of that kind (running tracks + orphans);
                                  0, or no entry, holds the whole kind.
  2. `limits.max_sessions`        shared cap on live sessions across kinds.
  3. `admit(st)`                  one shared gate for everything else: the controller passes
                                  disk.Gate.admit (the project disk budget, trackmanager/disk.py).
                                  It returns None to admit, or a string saying why not. It runs
                                  last, so a None return means the track is admitted and a stateful
                                  gate may reserve for it.
Order is priority (higher first), then the time the track was queued. A track that fails a
gate does not block later tracks (they may still fit).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import states as S


@dataclass
class Plan:
    submit: list = field(default_factory=list)        # states to submit now, in order
    waiting: dict = field(default_factory=dict)       # eb -> {"position": n, "blocked_by": str}
    in_use: dict = field(default_factory=dict)        # kind -> live sessions after this plan
    total_in_use: int = 0
    held: int = 0                                     # queued tracks with the operator HOLD flag


def kind_of(st):
    return S.WORKER_STAGE.get(st.get("stage"))


def is_queued(st) -> bool:
    return kind_of(st) is not None and st.get("activity") == S.QUEUED


def is_running(st) -> bool:
    return kind_of(st) is not None and st.get("activity") == S.RUNNING


def plan(states, limits, orphans=None, priority=lambda st: 0, admit=None) -> Plan:
    """`states`: every track's state dict. `orphans`: {kind: n} live sessions the database does
    not expect (they take a slot until they end). `priority(st)`: higher goes first."""
    p = Plan()
    in_use = {k: 0 for k in S.WORKER_KINDS}
    for st in states:
        if is_running(st):
            in_use[kind_of(st)] += 1
    for kind, n in (orphans or {}).items():
        in_use[kind] = in_use.get(kind, 0) + n
    total = sum(in_use.values())
    queued = [st for st in states if is_queued(st)]
    p.held = sum(bool(st.get("hold")) for st in queued)
    queue = sorted((st for st in queued if not st.get("hold")),
                   key=lambda st: (-int(priority(st) or 0), st.get("queued_at") or st.get("updated") or "",
                                   st["eb"]))
    position = 0
    for st in queue:
        kind = kind_of(st)
        cap = limits.max_workers.get(kind, 0)
        blocked = None
        if in_use[kind] >= cap:
            blocked = (f"max_workers.{kind} ({in_use[kind]}/{cap})" if kind in limits.max_workers
                       else f"max_workers.{kind} is not set")
        elif limits.max_sessions is not None and total >= limits.max_sessions:
            blocked = f"max_sessions ({total}/{limits.max_sessions})"
        elif admit is not None:
            blocked = admit(st) or None
        if blocked:
            position += 1
            p.waiting[st["eb"]] = {"position": position, "blocked_by": blocked}
            continue
        p.submit.append(st)
        in_use[kind] += 1
        total += 1
    p.in_use, p.total_in_use = in_use, total
    return p
