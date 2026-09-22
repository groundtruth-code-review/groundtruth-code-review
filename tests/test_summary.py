from groundtruth.quality_gate import Finding, GateVerdict, Severity
from groundtruth.summary import mentions_only_verified_files, summary_prompts, write_summary


def _verdict(file="invoice.py", line=2, title="Discount can exceed 100%"):
    finding = Finding(
        file=file, line=line, category="correctness", severity=Severity.HIGH,
        confidence=0.9, title=title, quoted_code="rate = sum(p.rate for p in promos)",
    )
    return GateVerdict(finding=finding, posted=True, fingerprint="fp", combined_confidence=0.81)


class FakeLlm:
    def __init__(self, response=None, raises=False):
        self._response = response
        self._raises = raises
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        if self._raises:
            raise RuntimeError("provider down")
        return self._response


def test_a_grounded_summary_is_returned():
    llm = FakeLlm({"summary": "One contract break in invoice.py."})
    assert write_summary([_verdict()], llm) == "One contract break in invoice.py."


def test_a_summary_naming_an_unverified_file_is_discarded():
    # the failure this stage must not have: inventing a location in the one
    # piece of text the gate never checked itself
    llm = FakeLlm({"summary": "Also check payments/refund.py for the same bug."})
    assert write_summary([_verdict()], llm) is None


def test_a_failed_call_falls_back_instead_of_raising():
    llm = FakeLlm(raises=True)
    assert write_summary([_verdict()], llm) is None


def test_an_empty_response_falls_back():
    assert write_summary([_verdict()], FakeLlm({"summary": "   "})) is None
    assert write_summary([_verdict()], FakeLlm({})) is None


def test_no_findings_means_no_call_at_all():
    llm = FakeLlm({"summary": "nothing to say"})
    assert write_summary([], llm) is None
    assert llm.calls == 0


def test_a_summary_with_no_file_reference_is_allowed():
    assert mentions_only_verified_files("Two related contract breaks.", [_verdict()]) is True


def test_version_numbers_are_not_mistaken_for_files():
    assert mentions_only_verified_files("Confidence 0.81 on Python 3.11.", [_verdict()]) is True


def test_a_bare_filename_matches_its_verified_path():
    verdict = _verdict(file="payments/invoice.py")
    assert mentions_only_verified_files("invoice.py changed a contract.", [verdict]) is True


def test_prompts_carry_every_finding_and_no_instructions_to_review():
    system, user = summary_prompts([_verdict(), _verdict(file="checkout.py", title="Second thing")])
    assert "NOT reviewing" in system
    assert "invoice.py:2" in user and "checkout.py:2" in user
    assert "Second thing" in user
