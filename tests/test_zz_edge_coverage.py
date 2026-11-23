"""Every edge of the state table that the controller can execute was exercised by a driver test.

Runs last (file name) and reads the events.jsonl of every driver run this pytest session made.
Skipped unless the whole driver suite was collected, since a partial run can't cover the table.
"""
import inspect

import pytest

import test_discovery
import test_fake_backend
import test_stages
import test_staging
from trackmanager import states as S
from trackmanager.store import read_jsonl


def test_every_non_pending_edge_was_observed(request, tmp_path_factory):
    wanted = {f"{m.__name__}::{n}" for m in (test_fake_backend, test_stages, test_discovery, test_staging)
              for n, f in inspect.getmembers(m, inspect.isfunction) if n.startswith("test_")}
    collected = {f"{i.module.__name__}::{i.originalname}" for i in request.session.items}
    if not wanted <= collected:
        pytest.skip("partial run: the driver suite was not fully collected")
    seen = set()
    for ev in tmp_path_factory.getbasetemp().glob("*/run/events.jsonl"):
        for e in read_jsonl(ev):
            if e.get("from") in S.STAGES and e.get("to") in S.STAGES and e["from"] != e["to"]:
                seen.add((e["from"], e["to"]))
    missing = [(f, t) for f, t, edge in S.edges() if not edge.pending and (f, t) not in seen]
    assert not missing, f"edges never exercised by a driver test: {missing}"
    unexpected = [(f, t) for f, t in seen if t not in S.TRANSITIONS[f]]
    assert not unexpected, f"events record edges that are not in the table: {unexpected}"
