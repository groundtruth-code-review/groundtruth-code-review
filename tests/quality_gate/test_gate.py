from groundtruth.quality_gate.gate import run_gate
from groundtruth.quality_gate.models import DropStage, Finding, Severity

EVIDENCE = [
    "def calculate_discount(self, price, promos):\n"
    "    rate = sum(p.rate for p in promos)\n"
    "    return price * rate"
]


def _hunk_text_for(_finding_obj) -> str:
    return EVIDENCE[0]


class FakeLlm:
    def __init__(self, response=None):
        self._response = response or {"is_real": True, "is_actionable": True, "confidence": 0.95}

    def complete_json(self, system, user):
        return self._response


def _finding(quoted_code="rate = sum(p.rate for p in promos)", confidence=0.9, category="correctness"):
    return Finding(
        file="invoice.py",
        line=2,
        category=category,
        severity=Severity.HIGH,
        confidence=confidence,
        title="Discount can exceed 100%",
        quoted_code=quoted_code,
    )


_HUNK_SPANS = [(1, 3)]


def _line_is_changed(finding_obj) -> bool:
    return any(start <= finding_obj.line <= end for start, end in _HUNK_SPANS)


def test_a_real_verified_finding_gets_posted():
    report = run_gate([_finding()], EVIDENCE, _hunk_text_for, FakeLlm())
    assert len(report.posted) == 1
    assert report.dropped == []


def test_hallucinated_quote_is_dropped_before_any_skeptic_call():
    llm = FakeLlm()
    finding = _finding(quoted_code="this text does not appear anywhere in the evidence")
    report = run_gate([finding], EVIDENCE, _hunk_text_for, llm)
    assert report.posted == []
    assert report.dropped[0].dropped_at == DropStage.HALLUCINATION


def test_already_seen_fingerprint_is_deduped():
    finding = _finding()
    from groundtruth.quality_gate.fingerprint import fingerprint as fp

    already_posted = {fp("invoice.py", _hunk_text_for(finding), "correctness", finding.quoted_code)}
    report = run_gate([finding], EVIDENCE, _hunk_text_for, FakeLlm(), seen_fingerprints=already_posted)
    assert report.posted == []
    assert report.dropped[0].dropped_at == DropStage.DEDUPE


def test_skeptic_rejection_drops_the_finding():
    llm = FakeLlm({"is_real": False, "is_actionable": False, "confidence": 0.9})
    report = run_gate([_finding()], EVIDENCE, _hunk_text_for, llm)
    assert report.posted == []
    assert report.dropped[0].dropped_at == DropStage.LOW_CONFIDENCE


def test_low_combined_confidence_is_dropped():
    # reviewer 0.9 x skeptic 0.5 = 0.45, below the 0.7 default bar
    llm = FakeLlm({"is_real": True, "is_actionable": True, "confidence": 0.5})
    report = run_gate([_finding(confidence=0.9)], EVIDENCE, _hunk_text_for, llm)
    assert report.posted == []
    assert report.dropped[0].dropped_at == DropStage.LOW_CONFIDENCE


def test_overflow_beyond_max_findings_is_dropped_not_posted():
    findings = [
        _finding(quoted_code="rate = sum(p.rate for p in promos)", category=f"cat{i}")
        for i in range(3)
    ]
    # each needs its own fingerprint (different category) so dedupe doesn't eat them first
    report = run_gate(findings, EVIDENCE, _hunk_text_for, FakeLlm(), max_findings=2)
    assert len(report.posted) == 2
    assert len(report.dropped) == 1


def test_two_distinct_findings_in_the_same_hunk_both_survive():
    evidence = ["def process(a, b):\n    x = a / b\n    y = risky_call(a)\n    return x, y"]
    findings = [
        Finding("proc.py", 2, "correctness", Severity.HIGH, 0.9, "Division by zero", "x = a / b"),
        Finding("proc.py", 3, "security", Severity.HIGH, 0.9, "Unvalidated call", "y = risky_call(a)"),
    ]
    report = run_gate(findings, evidence, lambda f: evidence[0], FakeLlm())
    assert len(report.posted) == 2


def test_dropped_finding_records_a_human_readable_reason():
    finding = _finding(quoted_code="this text does not appear anywhere")
    report = run_gate([finding], EVIDENCE, _hunk_text_for, FakeLlm())
    assert report.dropped[0].reason  # non-empty, not just a stage enum


def test_hunk_text_for_is_called_per_finding_not_once_for_all():
    # This is the actual DD-031-class regression: two findings in the same
    # file must be able to get DIFFERENT hunk text (and therefore stay
    # independently identified) instead of sharing one file-wide value.
    calls = []

    def hunk_text_for(finding):
        calls.append(finding.line)
        return f"hunk-for-line-{finding.line}"

    findings = [
        Finding("a.py", 5, "correctness", Severity.HIGH, 0.9, "Finding A", "code near line 5"),
        Finding("a.py", 50, "security", Severity.HIGH, 0.9, "Finding B", "code near line 50"),
    ]
    evidence = ["code near line 5", "code near line 50"]
    run_gate(findings, evidence, hunk_text_for, FakeLlm())
    assert calls == [5, 50]  # called once per finding, with that finding's own line


def test_a_real_quote_with_a_line_outside_the_diff_is_dropped():
    # the quote is genuine, the line number is not — this used to survive the
    # gate and then fail silently when the platform refused to place a
    # comment on a line that is not part of the diff
    finding = _finding()
    misplaced = Finding(**{**finding.__dict__, "line": 87})
    report = run_gate(
        [misplaced], EVIDENCE, _hunk_text_for, FakeLlm(), line_is_changed=_line_is_changed
    )
    assert report.posted == []
    assert report.dropped[0].dropped_at == DropStage.BAD_LOCATION
    assert "invoice.py:87" in report.dropped[0].reason


def test_a_line_inside_the_hunk_still_posts():
    report = run_gate(
        [_finding()], EVIDENCE, _hunk_text_for, FakeLlm(), line_is_changed=_line_is_changed
    )
    assert len(report.posted) == 1


def test_no_location_check_configured_means_no_location_drops():
    # a library user verifying claims against arbitrary evidence has no diff
    # to check a line against; the gate must not invent a failure for them
    misplaced = Finding(**{**_finding().__dict__, "line": 87})
    report = run_gate([misplaced], EVIDENCE, _hunk_text_for, FakeLlm())
    assert len(report.posted) == 1
