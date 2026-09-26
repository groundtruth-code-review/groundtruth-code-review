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
    # properties of the shipped suite, not its size -- a count pinned here
    # would break every time someone added a case
    main(["eval", "--cases", "cases", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["caught_total"] > 0
    assert payload["catch_rate"] == 1.0
    assert payload["gate_held_total"] > 0
    assert payload["gate_leaked_total"] == 0
    assert payload["gate_wrong_stage_total"] == 0
    assert payload["gate_unexercised_total"] == 0


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


# --- a gate case must hold for the right reason ---------------------------

def _gate_case(finding, stage, skeptic=None):
    return EvalCase(
        name="g", diff=_DIFF, head_sources={"pager.py": _HEAD},
        expect_gated=(ExpectedFinding("pager.py", 2, "off-by-one", stage=stage),),
        recorded_review={"findings": [finding]},
        recorded_skeptic=skeptic or {"is_real": True, "is_actionable": True, "confidence": 0.9},
    )


def test_a_gate_case_rejected_by_the_wrong_stage_fails():
    # the case says the location check must reject it; the quote check gets
    # there first. Right outcome, wrong reason -- and the location check was
    # never tested, so this must not count as a pass
    fabricated = {**_GOOD_FINDING, "quoted_code": "    end = page * per_page + 7777"}
    report = run_suite([_gate_case(fabricated, "bad_location")])
    assert report.gate_wrong_stage_total == 1
    assert report.gate_held_total == 0


def test_a_gate_case_rejected_by_the_right_stage_holds():
    fabricated = {**_GOOD_FINDING, "quoted_code": "    end = page * per_page + 7777"}
    report = run_suite([_gate_case(fabricated, "hallucination")])
    assert report.gate_held_total == 1
    assert report.gate_wrong_stage_total == 0


def test_eval_fails_on_a_wrong_stage(tmp_path, capsys):
    import json as _json
    fabricated = {**_GOOD_FINDING, "quoted_code": "    end = page * per_page + 7777"}
    (tmp_path / "c.json").write_text(_json.dumps({
        "name": "c", "diff": _DIFF, "head_sources": {"pager.py": _HEAD},
        "expect_gated": [{"file": "pager.py", "line": 2, "title_contains": "off-by-one",
                          "stage": "bad_location"}],
        "recorded": {"review": {"findings": [fabricated]}},
    }))
    assert main(["eval", "--cases", str(tmp_path)]) == 1
    assert "wrong reason" in capsys.readouterr().err


def test_a_gate_case_that_is_never_proposed_is_untested_not_held():
    # this used to count as held: the gate "passed" a test it never took
    report = run_suite([EvalCase(
        name="g", diff=_DIFF, head_sources={"pager.py": _HEAD},
        expect_gated=(ExpectedFinding("pager.py", 2, "off-by-one"),),
        recorded_review={"findings": []},
    )])
    assert report.gate_unexercised_total == 1
    assert report.gate_held_total == 0


# --- one diff, two findings, judged separately ----------------------------

def test_the_recorded_skeptic_can_answer_per_finding():
    from groundtruth.eval import RecordedLlm

    llm = RecordedLlm([
        {"match": "injection", "response": {"is_real": True, "confidence": 0.95}},
        {"match": "spacing", "response": {"is_real": False, "confidence": 0.1}},
        {"response": {"is_real": True, "confidence": 0.5}},
    ])
    assert llm.complete_json("s", "Finding: SQL injection here")["confidence"] == 0.95
    assert llm.complete_json("s", "Finding: missing spacing")["confidence"] == 0.1
    assert llm.complete_json("s", "Finding: something else")["confidence"] == 0.5


def test_a_single_recorded_answer_still_answers_everything():
    from groundtruth.eval import RecordedLlm

    llm = RecordedLlm({"is_real": True, "confidence": 0.7})
    assert llm.complete_json("s", "anything")["confidence"] == 0.7


# --- --live uses real models and only the recall cases --------------------

def test_live_mode_builds_clients_from_config_and_skips_pipeline_cases(tmp_path, monkeypatch, capsys):
    import groundtruth.cli as cli_module

    built = []

    class RecordingClient:
        def __init__(self, model, api_base=None, **kwargs):
            built.append(model)

        def complete_json(self, system, user):
            if "skeptical staff engineer" in system.lower():
                return {"is_real": True, "is_actionable": True, "confidence": 0.9}
            return {"findings": []}

    monkeypatch.setattr(cli_module, "LlmClient", RecordingClient)
    config = tmp_path / ".groundtruth.yml"
    config.write_text("model: anthropic/claude-sonnet-5\nverify_model: openai/gpt-4o-mini\n")

    main(["eval", "--cases", "cases", "--live", "--config", str(config), "--format", "json"])
    out = capsys.readouterr()

    assert built == ["anthropic/claude-sonnet-5", "openai/gpt-4o-mini"]
    payload = __import__("json").loads(out.out)
    names = {c["name"] for c in payload["cases"]}
    assert not any(n.startswith("gate_") for n in names), "pipeline cases must not run live"
    assert "billed" in out.err


def test_live_mode_can_override_the_verifier_to_compare_providers(tmp_path, monkeypatch, capsys):
    import groundtruth.cli as cli_module

    built = []

    class RecordingClient:
        def __init__(self, model, api_base=None, **kwargs):
            built.append(model)

        def complete_json(self, system, user):
            return {"findings": []}

    monkeypatch.setattr(cli_module, "LlmClient", RecordingClient)
    config = tmp_path / ".groundtruth.yml"
    config.write_text("model: anthropic/claude-sonnet-5\n")

    main(["eval", "--cases", "cases", "--live", "--config", str(config),
          "--verify-model", "openai/gpt-4o-mini", "--format", "json"])
    capsys.readouterr()
    assert built == ["anthropic/claude-sonnet-5", "openai/gpt-4o-mini"]


def test_every_shipped_case_declares_its_track_and_purpose():
    for case in load_cases("cases"):
        assert case.track in {"recall", "pipeline"}, case.name
        assert case.about, f"{case.name} does not say what it proves"


def test_every_shipped_gate_case_names_its_stage():
    for case in load_cases("cases"):
        for expected in case.expect_gated:
            assert expected.stage, f"{case.name} does not say which stage must reject it"


def test_a_live_run_with_failed_calls_is_not_reported_as_a_bad_model(capsys, monkeypatch, tmp_path):
    # a missing key makes every call fail; that must read as "calls failed",
    # never as "the model missed every bug"
    import groundtruth.cli as cli_module

    class FailingClient:
        def __init__(self, model, api_base=None, **kwargs):
            pass

        def complete_json(self, system, user):
            raise RuntimeError("missing API key")

    monkeypatch.setattr(cli_module, "LlmClient", FailingClient)
    assert main(["eval", "--cases", "cases", "--live", "--config", str(tmp_path / "none.yml")]) == 1
    out = capsys.readouterr()
    assert "never answered" in out.out
    assert "does not measure the model" in out.err


def test_an_offline_run_has_no_failed_calls():
    report = run_suite(load_cases("cases"))
    assert report.review_calls_total > 0
    assert report.review_failures_total == 0


def test_live_eval_refuses_a_misprefixed_nvidia_model_before_any_call(tmp_path, monkeypatch, capsys):
    import groundtruth.cli as cli_module

    def no_calls(*args, **kwargs):
        raise AssertionError("no client should be built for a config that cannot work")

    monkeypatch.setattr(cli_module, "LlmClient", no_calls)
    config = tmp_path / "c.yml"
    config.write_text("model: openai/gpt-oss-20b\n")
    code = main(["eval", "--cases", "cases", "--live", "--config", str(config),
                 "--base-url", "https://integrate.api.nvidia.com/v1"])
    assert code == 1
    assert "nvidia_nim/openai/gpt-oss-20b" in capsys.readouterr().err
