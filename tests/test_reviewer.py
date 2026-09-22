import logging

from groundtruth.context_engine import parse_diff
from groundtruth.llm import CostEstimate
from groundtruth.quality_gate.models import Severity
from groundtruth.reviewer import propose_findings, propose_findings_for_diffmap


class FakeLlm:
    def __init__(self, response=None, raises=False):
        self._response = response
        self._raises = raises
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if self._raises:
            raise RuntimeError("simulated outage")
        return self._response

    def estimate_cost(self, system, user, expected_completion_tokens=800):
        return CostEstimate(
            model="fake", prompt_tokens=len(user) // 4,
            estimated_completion_tokens=expected_completion_tokens, estimated_cost_usd=0.01,
        )


class FakeLlmNoEstimate:
    """A minimal LLM double implementing ONLY complete_json — the narrowest
    thing propose_findings actually requires. Used to prove prompt-size
    logging is optional infrastructure, not a hard dependency of a review.
    """

    def __init__(self, response=None):
        self._response = response or {"findings": []}

    def complete_json(self, system, user):
        return self._response


VALID_RESPONSE = {
    "findings": [
        {
            "file": "invoice.py",
            "line": 2,
            "category": "correctness",
            "severity": "high",
            "confidence": 0.9,
            "title": "Discount can exceed 100%",
            "quoted_code": "rate = sum(p.rate for p in promos)",
        }
    ]
}


def test_parses_a_real_finding():
    llm = FakeLlm(VALID_RESPONSE)
    findings = propose_findings("diff text", [], llm)
    assert len(findings) == 1
    f = findings[0]
    assert f.file == "invoice.py"
    assert f.severity == Severity.HIGH
    assert f.confidence == 0.9


def test_empty_findings_list_is_fine():
    llm = FakeLlm({"findings": []})
    assert propose_findings("diff text", [], llm) == []


def test_broken_llm_call_degrades_to_empty_not_a_crash():
    llm = FakeLlm(raises=True)
    assert propose_findings("diff text", [], llm) == []


def test_malformed_individual_finding_is_dropped_not_fatal():
    malformed = {
        "file": "a.py",
        "line": "not-a-number",
        "category": "x",
        "severity": "high",
        "confidence": 0.9,
        "title": "t",
        "quoted_code": "q",
    }
    response = {"findings": [malformed, VALID_RESPONSE["findings"][0]]}
    llm = FakeLlm(response)
    findings = propose_findings("diff text", [], llm)
    assert len(findings) == 1  # the malformed one dropped, the valid one survives


def test_unknown_severity_string_drops_that_finding():
    response = {
        "findings": [
            {**VALID_RESPONSE["findings"][0], "severity": "catastrophic"},
        ]
    }
    llm = FakeLlm(response)
    assert propose_findings("diff text", [], llm) == []


def test_confidence_is_clamped_to_0_1_range():
    response = {"findings": [{**VALID_RESPONSE["findings"][0], "confidence": 1.5}]}
    llm = FakeLlm(response)
    findings = propose_findings("diff text", [], llm)
    assert findings[0].confidence == 1.0


def test_dimensions_are_passed_into_the_system_prompt():
    llm = FakeLlm({"findings": []})
    propose_findings("diff text", [], llm, dimensions=["security"])
    system_prompt_used = llm.calls[0][0]
    assert "security" in system_prompt_used


TWO_FILE_DIFF = (
    "diff --git a/invoice.py b/invoice.py\n"
    "--- a/invoice.py\n"
    "+++ b/invoice.py\n"
    "@@ -1,1 +1,2 @@\n"
    " def calculate_discount(price):\n"
    "+    return price * 0.9\n"
    "diff --git a/checkout.py b/checkout.py\n"
    "--- a/checkout.py\n"
    "+++ b/checkout.py\n"
    "@@ -1,1 +1,2 @@\n"
    " def checkout(price):\n"
    "+    return calculate_discount(price)\n"
)


class PerFileFakeLlm:
    """Returns a different, scripted response depending on which file's
    diff snippet is in the prompt — the point being each call only ever
    sees ONE file's diff, never the whole multi-file PR at once.
    """

    def __init__(self, responses_by_file: dict[str, dict], raise_for: set[str] | None = None):
        self._responses_by_file = responses_by_file
        self._raise_for = raise_for or set()
        self.calls = []

    def estimate_cost(self, system, user, expected_completion_tokens=800):
        return CostEstimate(
            model="fake", prompt_tokens=len(user) // 4,
            estimated_completion_tokens=expected_completion_tokens, estimated_cost_usd=0.01,
        )

    def complete_json(self, system, user):
        self.calls.append(user)
        for path, response in self._responses_by_file.items():
            if f"+++ b/{path}" in user:
                if path in self._raise_for:
                    raise RuntimeError(f"simulated outage reviewing {path}")
                # A real single-file call must never see the OTHER file's diff.
                other_paths = set(self._responses_by_file) - {path}
                assert not any(f"+++ b/{other}" in user for other in other_paths), (
                    f"call for {path} leaked another file's diff into its prompt"
                )
                return response
        raise AssertionError(f"no scripted response matched this call's file:\n{user}")


def test_makes_one_review_call_per_changed_file():
    diffmap = parse_diff(TWO_FILE_DIFF)
    llm = PerFileFakeLlm({"invoice.py": {"findings": []}, "checkout.py": {"findings": []}})
    propose_findings_for_diffmap(diffmap, [], llm)
    assert len(llm.calls) == 2


def test_findings_from_every_file_are_combined_not_just_the_first():
    diffmap = parse_diff(TWO_FILE_DIFF)
    invoice_finding = {**VALID_RESPONSE["findings"][0], "file": "invoice.py"}
    checkout_finding = {
        "file": "checkout.py", "line": 2, "category": "correctness", "severity": "medium",
        "confidence": 0.8, "title": "Missing promos argument",
        "quoted_code": "return calculate_discount(price)",
    }
    llm = PerFileFakeLlm({
        "invoice.py": {"findings": [invoice_finding]},
        "checkout.py": {"findings": [checkout_finding]},
    })
    findings = propose_findings_for_diffmap(diffmap, [], llm)
    assert {f.file for f in findings} == {"invoice.py", "checkout.py"}


def test_one_files_call_failing_does_not_lose_the_others_findings():
    diffmap = parse_diff(TWO_FILE_DIFF)
    checkout_finding = {
        "file": "checkout.py", "line": 2, "category": "correctness", "severity": "medium",
        "confidence": 0.8, "title": "Missing promos argument",
        "quoted_code": "return calculate_discount(price)",
    }
    llm = PerFileFakeLlm(
        {"invoice.py": {"findings": []}, "checkout.py": {"findings": [checkout_finding]}},
        raise_for={"invoice.py"},
    )
    findings = propose_findings_for_diffmap(diffmap, [], llm)
    assert len(findings) == 1
    assert findings[0].file == "checkout.py"


def test_prompt_size_is_logged_when_the_llm_can_measure_it(caplog):
    with caplog.at_level(logging.INFO, logger="groundtruth.reviewer"):
        propose_findings("some diff text", [], FakeLlm({"findings": []}), label="invoice.py")

    records = [r for r in caplog.records if r.message.startswith("review_call_prompt_size")]
    assert len(records) == 1
    assert "file=invoice.py" in records[0].message
    assert "diff_tokens=" in records[0].message
    assert "context_tokens=" in records[0].message
    assert "combined_tokens=" in records[0].message


def test_unlabeled_call_logs_a_placeholder_not_a_blank(caplog):
    with caplog.at_level(logging.INFO, logger="groundtruth.reviewer"):
        propose_findings("some diff text", [], FakeLlm({"findings": []}))

    assert "file=(unlabeled)" in caplog.text


def test_prompt_size_logging_failure_never_breaks_the_review(caplog):
    with caplog.at_level(logging.INFO, logger="groundtruth.reviewer"):
        findings = propose_findings(
            "some diff text", [], FakeLlmNoEstimate({"findings": [VALID_RESPONSE["findings"][0]]})
        )

    # No estimate_cost on this double at all -> measurement silently skipped,
    # but the actual review still ran and returned its real finding.
    assert not any(r.message.startswith("review_call_prompt_size") for r in caplog.records)
    assert len(findings) == 1


def test_batched_review_logs_one_size_line_per_file(caplog):
    diffmap = parse_diff(TWO_FILE_DIFF)
    llm = PerFileFakeLlm({"invoice.py": {"findings": []}, "checkout.py": {"findings": []}})

    with caplog.at_level(logging.INFO, logger="groundtruth.reviewer"):
        propose_findings_for_diffmap(diffmap, [], llm)

    size_lines = [r.message for r in caplog.records if r.message.startswith("review_call_prompt_size")]
    assert len(size_lines) == 2
    assert any("file=invoice.py" in line for line in size_lines)
    assert any("file=checkout.py" in line for line in size_lines)


# --- the diff half of the prompt has a budget too -------------------------

def _diff_with_hunks(count: int, lines_per_hunk: int = 40) -> str:
    parts = ["diff --git a/big.py b/big.py", "--- a/big.py", "+++ b/big.py"]
    for i in range(count):
        start = 1 + i * 100
        parts.append(f"@@ -{start},1 +{start},{lines_per_hunk} @@")
        parts.extend(f"+    value_{i}_{j} = compute_something_with_a_long_name({j})"
                     for j in range(lines_per_hunk))
    return "\n".join(parts) + "\n"


def test_a_file_bigger_than_the_budget_is_reviewed_in_groups():
    from groundtruth.context_engine import parse_diff
    from groundtruth.reviewer import hunk_groups

    diffmap = parse_diff(_diff_with_hunks(6))
    groups = hunk_groups(diffmap["big.py"], max_tokens=600)
    assert len(groups) > 1
    # every hunk survives somewhere: splitting must never drop code from review
    assert sum(len(g) for g in groups) == len(diffmap["big.py"].hunks)


def test_a_single_oversized_hunk_still_goes_out_whole():
    from groundtruth.context_engine import parse_diff
    from groundtruth.reviewer import hunk_groups

    diffmap = parse_diff(_diff_with_hunks(1, lines_per_hunk=200))
    groups = hunk_groups(diffmap["big.py"], max_tokens=100)
    assert len(groups) == 1 and len(groups[0]) == 1


def test_a_small_file_stays_one_call():
    from groundtruth.context_engine import parse_diff
    from groundtruth.reviewer import hunk_groups

    diffmap = parse_diff(_diff_with_hunks(2, lines_per_hunk=3))
    assert len(hunk_groups(diffmap["big.py"], max_tokens=6000)) == 1
