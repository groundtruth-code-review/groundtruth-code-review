import json

from groundtruth.cli import build_arg_parser, main
from groundtruth.eval import EvalCase, ExpectedFinding, RecordedLlm, load_cases, run_case, run_suite

_HEAD = "def page_items(items, page, per_page):\n    end = page * per_page + 1\n    return items[:end]\n"
_DIFF = (
    "diff --git a/pager.py b/pager.py\n"
    "--- a/pager.py\n"
    "+++ b/pager.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def page_items(items, page, per_page):\n"
    "-    end = page * per_page\n"
    "+    end = page * per_page + 1\n"
    "     return items[:end]\n"
)


def _case(review, skeptic=None, expected=(("pager.py", 2, "off-by-one"),)):
    return EvalCase(
        name="case",
        diff=_DIFF,
        head_sources={"pager.py": _HEAD},
        base_sources={"pager.py": _HEAD.replace(" + 1", "")},
        expected_findings=tuple(ExpectedFinding(f, ln, t) for f, ln, t in expected),
        recorded_review=review,
        recorded_skeptic=skeptic or {"is_real": True, "is_actionable": True, "confidence": 0.9},
    )


_GOOD_FINDING = {
    "file": "pager.py", "line": 2, "category": "correctness", "severity": "medium",
    "confidence": 0.9, "title": "off-by-one: one item too many",
    "quoted_code": "    end = page * per_page + 1",
}


def test_a_posted_expected_finding_is_caught():
    result = run_case(_case({"findings": [_GOOD_FINDING]}))
    assert len(result.caught) == 1
    assert result.gated == [] and result.missed == [] and result.false_positives == []


def test_a_finding_the_gate_dropped_is_gated_not_missed():
    # the distinction the harness exists for: proposed but rejected is a gate
    # problem, never proposed is a prompt or context problem
    fabricated = {**_GOOD_FINDING, "quoted_code": "    end = page * per_page + 7777"}
    result = run_case(_case({"findings": [fabricated]}))
    assert result.caught == [] and result.missed == []
    assert result.gated[0][1] == "hallucination"


def test_a_low_confidence_drop_records_that_stage():
    doubtful = {"is_real": True, "is_actionable": True, "confidence": 0.2}
    result = run_case(_case({"findings": [_GOOD_FINDING]}, skeptic=doubtful))
    assert result.gated[0][1] == "low_confidence"


def test_a_finding_never_proposed_is_missed():
    result = run_case(_case({"findings": []}))
    assert len(result.missed) == 1
    assert result.caught == [] and result.gated == []


def test_an_unexpected_posted_finding_is_a_false_positive():
    result = run_case(_case({"findings": [_GOOD_FINDING]}, expected=()))
    assert len(result.false_positives) == 1
    assert result.expected_total == 0


def test_rates_over_a_suite():
    report = run_suite([
        _case({"findings": [_GOOD_FINDING]}),
        _case({"findings": []}),
    ])
    assert report.catch_rate == 0.5
    assert report.false_positive_rate == 0.0


def test_an_empty_suite_scores_a_full_catch_rate():
    report = run_suite([])
    assert report.catch_rate == 1.0
    assert report.false_positive_total == 0


def test_a_recorded_response_is_replayed_without_a_network_call():
    llm = RecordedLlm({"findings": []})
    run_case(_case({"findings": []}), review_llm=llm)
    assert llm.calls == 1


# --- the shipped cases and the CI gate ------------------------------------

def test_the_shipped_cases_load_and_pass_their_own_thresholds(capsys):
    cases = load_cases("cases")
    assert len(cases) >= 4
    report = run_suite(cases)
    assert report.false_positive_total == 0
    # a gated-by-design case no longer sits in the denominator, so a real
    # catch rate is reachable and the CI threshold can mean something
    assert report.catch_rate == 1.0
    assert report.gate_leaked_total == 0


def test_eval_exits_nonzero_when_the_catch_rate_is_too_low(tmp_path, capsys):
    # a case whose bug is never proposed, so the catch rate really is 0 --
    # the shipped suite now scores 1.0, so it cannot demonstrate this
    import json as _json
    case = {
        "name": "missed", "diff": _DIFF, "head_sources": {"pager.py": _HEAD},
        "expected_findings": [{"file": "pager.py", "line": 2, "title_contains": "off-by-one"}],
        "recorded": {"review": {"findings": []},
                     "skeptic": {"is_real": True, "is_actionable": True, "confidence": 0.9}},
    }
    (tmp_path / "missed.json").write_text(_json.dumps(case))
    assert main(["eval", "--cases", str(tmp_path), "--min-catch", "0.8"]) == 1
    assert "below --min-catch" in capsys.readouterr().err


def test_eval_exits_zero_when_thresholds_are_met():
    assert main(["eval", "--cases", "cases", "--min-catch", "0.8", "--max-fp", "0.2"]) == 0


def test_eval_json_output_has_the_totals(capsys):
    main(["eval", "--cases", "cases", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["caught_total"] == 2
    assert payload["catch_rate"] == 1.0
    assert payload["gate_held_total"] == 1
    assert payload["gate_leaked_total"] == 0


def test_eval_reports_a_missing_case_directory(capsys):
    assert main(["eval", "--cases", "nope"]) == 1
    assert "no cases found" in capsys.readouterr().err


def test_the_parser_knows_both_commands():
    parser = build_arg_parser()
    assert parser.parse_args(["eval", "--cases", "cases"]).command == "eval"
    assert parser.parse_args(["review", "--base", "main"]).command == "review"


# --- gate assertions are graded apart from recall -------------------------

def test_a_gated_by_design_case_does_not_cap_the_catch_rate():
    # the bug this replaced: a case that exists to prove the gate works sat
    # in the catch-rate denominator, so the rate could never reach 1.0 and
    # --min-catch was unusable as a CI gate
    fabricated = {**_GOOD_FINDING, "quoted_code": "    end = page * per_page + 7777"}
    case = EvalCase(
        name="gate", diff=_DIFF, head_sources={"pager.py": _HEAD},
        expected_findings=(),
        expect_gated=(ExpectedFinding("pager.py", 2, "off-by-one"),),
        recorded_review={"findings": [fabricated]},
        recorded_skeptic={"is_real": True, "is_actionable": True, "confidence": 0.9},
    )
    report = run_suite([case])
    assert report.catch_rate == 1.0          # nothing was expected to be caught
    assert report.gate_held_total == 1
    assert report.gate_leaked_total == 0


def test_a_leak_is_reported_when_the_gate_lets_it_through():
    # same case, but the finding is real enough to survive -- the gate
    # assertion must fail rather than pass silently
    case = EvalCase(
        name="leak", diff=_DIFF, head_sources={"pager.py": _HEAD},
        expected_findings=(),
        expect_gated=(ExpectedFinding("pager.py", 2, "off-by-one"),),
        recorded_review={"findings": [_GOOD_FINDING]},
        recorded_skeptic={"is_real": True, "is_actionable": True, "confidence": 0.9},
    )
    report = run_suite([case])
    assert report.gate_leaked_total == 1
    assert report.gate_held_total == 0


def test_eval_fails_on_a_gate_leak_with_no_threshold_to_tune(tmp_path, capsys):
    import json as _json
    case = {
        "name": "leak", "diff": _DIFF, "head_sources": {"pager.py": _HEAD},
        "expected_findings": [],
        "expect_gated": [{"file": "pager.py", "line": 2, "title_contains": "off-by-one"}],
        "recorded": {"review": {"findings": [_GOOD_FINDING]},
                     "skeptic": {"is_real": True, "is_actionable": True, "confidence": 0.9}},
    }
    (tmp_path / "leak.json").write_text(_json.dumps(case))
    assert main(["eval", "--cases", str(tmp_path)]) == 1
    assert "leaked through the gate" in capsys.readouterr().err
