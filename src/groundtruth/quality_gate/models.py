"""Shared data shapes for the quality gate.

Kept separate from any one stage so `hallucination.py`, `fingerprint.py`,
`skeptic.py`, and `gate.py` all speak the same `Finding`/`GateVerdict`
vocabulary without importing each other in a circle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


_SEVERITY_WEIGHT = {Severity.HIGH: 4, Severity.MEDIUM: 3, Severity.LOW: 2, Severity.INFO: 1}


@dataclass(frozen=True)
class Finding:
    """One candidate problem reported by the review pass, before verification."""

    file: str
    line: int
    category: str  # e.g. "correctness", "security", "conventions"
    severity: Severity
    confidence: float  # the reviewer model's own self-assessment, 0..1
    title: str
    quoted_code: str  # the exact evidence this finding is about — must exist in the diff


class DropStage(str, Enum):
    HALLUCINATION = "hallucination"
    BAD_LOCATION = "bad_location"
    DEDUPE = "dedupe"
    LOW_CONFIDENCE = "low_confidence"


@dataclass(frozen=True)
class GateVerdict:
    """The outcome of running one Finding through the gate."""

    finding: Finding
    posted: bool
    fingerprint: str
    combined_confidence: float | None = None  # None if dropped before the skeptic pass ran
    dropped_at: DropStage | None = None
    reason: str = ""
    # What the skeptic actually answered, kept apart from the product above.
    # "combined 0.64" alone cannot say whether the verifier called the claim
    # false or agreed with it at 0.8 -- and those need opposite fixes.
    verifier_confidence: float | None = None
    verifier_real: bool | None = None
    verifier_actionable: bool | None = None

    @property
    def score(self) -> float:
        if self.combined_confidence is None:
            return 0.0
        return _SEVERITY_WEIGHT[self.finding.severity] * self.combined_confidence
