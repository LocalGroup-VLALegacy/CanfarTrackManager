"""Preflight: what can be decided about an EB from TAP metadata and the project cfgs, before
anything is downloaded (plan §3.7). A missing target in the cfgs otherwise kills the run at
`target_setup.py:69` after a ~500 GB download.

Levels: fail (the operator fixes a cfg, then `recheck`), skip_candidate (a human decides: short or
empty EBs, or no science field; an operator `include` or archive.include_ebs bypasses it), warn, info.
TAP has no scan intents, so fields are classified by name: a field matching a `vsys` target is a
target; one matching a calibrator cfg section, or that looks like a calibrator name, is a
calibrator (the name pattern is only a hint); anything else is unclassified and fails preflight.
`Scan.xml` confirms the split once the SDM is on disk (M3).
"""
from __future__ import annotations

import dataclasses as dc
import re
from collections import Counter

FAIL, SKIP, WARN, INFO = "fail", "skip_candidate", "warn", "info"
NEAR_ZERO_GB = 1.0                 # "near-zero size": below this the EB is a skip candidate
HI_GHZ = 1.4204                    # a calibrator needs an HI range when the band covers this
CALIBRATOR_HINTS = (re.compile(r"^3C\s?\d+", re.I), re.compile(r"^J\d{4}[+-]\d{4}"),
                    re.compile(r"^\d{4}[+-]\d{3}"))


@dc.dataclass
class Check:
    check: str
    level: str
    detail: str


@dc.dataclass
class Result:
    checks: list = dc.field(default_factory=list)
    fields: dict = dc.field(default_factory=dict)      # field name -> target | calibrator | unclassified
    targets: list = dc.field(default_factory=list)     # vsys keys matched

    def level(self, level):
        return [c for c in self.checks if c.level == level]

    @property
    def verdict(self) -> str:
        if self.level(FAIL):
            return "preflight_failed"
        return "skip_candidate" if self.level(SKIP) else "ok"

    def summary(self) -> str:
        bad = self.level(FAIL) or self.level(SKIP)
        text = "; ".join(f"{c.check}: {c.detail}" for c in bad)
        warns = len(self.level(WARN))
        return (text or "ok") + (f" ({warns} warning{'s' * (warns != 1)})" if warns else "")

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "fields": self.fields, "targets": self.targets,
                "checks": [dc.asdict(c) for c in self.checks]}


def match_target(field: str, vsys) -> str | None:
    """The pipeline's rule (`target_setup.identify_targets`, lines 59-65): the first vsys key that
    is a substring of the field name. tests/test_preflight.py checks that rule is still there."""
    for gal in vsys:
        if gal in field:
            return gal
    return None


def is_calibrator(field: str, tables) -> bool:
    return field in tables.calibrator_names() or any(h.match(field) for h in CALIBRATOR_HINTS)


def classify(fields, tables) -> dict:
    out = {}
    for f in fields:
        if match_target(f, tables.vsys):
            out[f] = "target"
        elif is_calibrator(f, tables):
            out[f] = "calibrator"
        else:
            out[f] = "unclassified"
    return out


def main_signature(records) -> str | None:
    """The project's usual correlator setup: the most common spw_signature."""
    sigs = Counter(r["spw_signature"] for r in records)
    return sigs.most_common(1)[0][0] if sigs else None


def run(rec: dict, cfg, tables, included: bool = False, main_sig: str | None = None) -> Result:
    res = Result()
    add = lambda check, level, detail: res.checks.append(Check(check, level, detail))
    res.fields = classify(rec["fields"], tables)
    res.targets = sorted({match_target(f, tables.vsys) for f, k in res.fields.items() if k == "target"})
    unclassified = [f for f, k in res.fields.items() if k == "unclassified"]
    if unclassified:
        add("fields", FAIL, f"{', '.join(unclassified)} match neither the targets (vsys) nor the "
                            "calibrators: add them to the project cfgs")
    elif not res.targets and not included:
        # e.g. a 5-minute EB that only observed 3C48: not a cfg problem, so a human gate
        add("targets", SKIP, "no science field in this EB (calibrators only)")
    no_vrange = [t for t in res.targets if t not in tables.vrange]
    if no_vrange:
        add("vrange", WARN, f"no protected velocity range for {', '.join(no_vrange)}")
    lo, hi = rec.get("freq_min_ghz"), rec.get("freq_max_ghz")
    if lo is not None and hi is not None and lo <= HI_GHZ <= hi:
        names = tables.calibrator_names()
        nohi = [f for f, k in res.fields.items() if k == "calibrator" and f not in names]
        if nohi:
            add("calibrator_hi", WARN, f"no HI range in the cfg for {', '.join(nohi)}")
    if not included:
        if rec["duration_min"] < cfg.archive.min_duration_min:
            add("duration", SKIP, f"{rec['duration_min']:.0f} min < min_duration_min "
                                  f"({cfg.archive.min_duration_min:g})")
        if rec["est_size_gb"] < NEAR_ZERO_GB:
            add("size", SKIP, f"estimated size {rec['est_size_gb']:.2f} GB")
    if rec.get("spw_varies"):
        add("spw_setup", WARN, "the SPW setup changes within the EB")
    elif main_sig and rec["spw_signature"] != main_sig:
        add("spw_setup", WARN, "SPW setup differs from the project's main setup")
    if rec.get("archive_status") and rec["archive_status"] != "PUBLIC":
        add("access", INFO, f"{rec['archive_status']}: the request needs a login with data rights")
    return res
