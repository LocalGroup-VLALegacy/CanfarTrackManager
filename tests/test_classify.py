"""Failure classes for a failed attempt (stages/workers.classify). The OOM case is from a real CANFAR
kill (2026-11-22, 1 GB request): Failed status, no run.json, no OOM text anywhere, the worker's last
memory sample 768 MiB of 1 GiB."""
from trackmanager.stages.workers import classify

GIB = 2**30


def test_oom_is_inferred_from_the_workers_last_memory_sample_when_the_platform_says_nothing():
    quiet = "Normal Scheduled ... Normal Started container ..."          # what the real events looked like
    assert classify("Failed", {}, quiet, peak_anon_bytes=int(0.85 * GIB), ram_gb=1) == "oom"
    assert classify("Failed", {}, quiet, peak_anon_bytes=int(0.5 * GIB), ram_gb=1) == "failed"
    assert classify("Failed", {}, quiet, peak_anon_bytes=None, ram_gb=1) == "failed"
    assert classify("Failed", {}, quiet, peak_anon_bytes=GIB, ram_gb=None) == "failed"      # flexible request


def test_a_recorded_exit_code_wins_over_memory_evidence():
    assert classify("Failed", {"exit_code": 1}, "", peak_anon_bytes=GIB, ram_gb=1) == "failed"
    assert classify("Completed", {"exit_code": 0}, "", peak_anon_bytes=GIB, ram_gb=1) == "no_output"


def test_platform_text_still_counts_and_the_other_classes_are_unchanged():
    assert classify("Failed", {}, "Reason: OOMKilled") == "oom"
    assert classify("Failed", {}, "pod exceeded its Deadline") == "deadline"
    assert classify("Failed", {}, "") == "failed"
    assert classify(None, {}, "") == "lost"
