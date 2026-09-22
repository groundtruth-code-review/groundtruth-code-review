import pytest

from groundtruth.quality_gate.models import Finding, Severity
from groundtruth.quality_gate.skeptic import SkepticVerdict, combined_confidence, cross_examine


class FakeLlm:
    """Test double for anything satisfying the JsonLlm protocol — no network,
    no key, just a canned response (or a forced failure)."""

    def __init__(self, response: dict | None = None, raises: bool = False):
        self._response = response or {}
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        if self._raises:
            raise RuntimeError("simulated provider outage")
        return self._response


def _finding(confidence=0.9) -> Finding:
    return Finding(
        file="invoice.py",
        line=3,
        category="correctness",
        severity=Severity.HIGH,
        confidence=confidence,
        title="Discount can exceed 100%",
        quoted_code="rate = sum(p.rate for p in promos)",
    )


def test_cross_examine_parses_a_real_verdict():
    llm = FakeLlm({"is_real": True, "is_actionable": True, "confidence": 0.95, "reason": "confirmed"})
    verdict = cross_examine(_finding(), "evidence text", llm)
    assert verdict == SkepticVerdict(is_real=True, is_actionable=True, confidence=0.95, reason="confirmed")


def test_a_broken_skeptic_call_degrades_to_no_confidence_not_a_crash():
    llm = FakeLlm(raises=True)
    verdict = cross_examine(_finding(), "evidence text", llm)
    assert verdict.is_real is False
    assert verdict.confidence == 0.0


def test_multiply_not_average_a_confident_wrong_answer_cannot_win():
    # Reviewer is very confident (0.9); skeptic is doubtful (0.5) but still
    # technically says "real" and "actionable." Averaging would give 0.7 and
    # clear a 0.7 bar. Multiplying gives 0.45 and correctly fails it.
    reviewer_confidence = 0.9
    verdict = SkepticVerdict(is_real=True, is_actionable=True, confidence=0.5)
    combined = combined_confidence(reviewer_confidence, verdict)
    assert combined == 0.45
    assert combined < 0.7  # the bar this system uses by default


def test_is_real_false_zeroes_the_finding_regardless_of_confidence():
    verdict = SkepticVerdict(is_real=False, is_actionable=True, confidence=0.99)
    assert combined_confidence(0.99, verdict) == 0.0


def test_is_actionable_false_zeroes_the_finding_regardless_of_confidence():
    verdict = SkepticVerdict(is_real=True, is_actionable=False, confidence=0.99)
    assert combined_confidence(0.99, verdict) == 0.0


def test_two_high_confidence_verdicts_do_clear_the_bar():
    verdict = SkepticVerdict(is_real=True, is_actionable=True, confidence=0.9)
    assert combined_confidence(0.9, verdict) == pytest.approx(0.81)
