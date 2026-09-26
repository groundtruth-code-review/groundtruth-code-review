import json
import subprocess

import pytest

from groundtruth.cli import (
    CostCeilingExceeded,
    ReviewOutcome,
    _windowed_hunk_text,
    render_json,
    render_text,
    run_review,
)
from groundtruth.config import Config
from groundtruth.llm import CostEstimate
from groundtruth.quality_gate import DropStage


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "invoice.py").write_text("def calculate_discount(price):\n    return price * 0.9\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    (tmp_path / "invoice.py").write_text(
        "def calculate_discount(price, promos):\n"
        "    rate = sum(p.rate for p in promos)\n"
        "    return price * rate\n"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "head")

    return tmp_path, base_sha


def _role_of(system: str) -> str:
    """Which pipeline role a prompt belongs to.

    Checked in priority order against each prompt's own opening line, not by
    looking for a loose keyword: the summary prompt talks about "findings"
    too, and a fake that matched on that word reported summary calls as
    review calls.
    """
    head = system.lower()
    if "skeptical staff engineer" in head:
        return "skeptic"
    if "writing the summary comment" in head:
        return "summary"
    return "review"


class FakeLlm:
    """Satisfies both the JsonLlm protocol (complete_json) and the subset of
    LlmClient the CLI calls (estimate_cost) — no network, no key.
    """

    def __init__(self, review_response=None, skeptic_response=None, summary_response=None, cost=0.01):
        self.review_response = review_response or {"findings": []}
        self.summary_response = summary_response or {"summary": "One verified finding."}
        self.skeptic_response = skeptic_response or {
            "is_real": True,
            "is_actionable": True,
            "confidence": 0.95,
        }
        self._cost = cost
        self.calls = 0
        self.review_calls = 0
        self.skeptic_calls = 0
        self.summary_calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        role = _role_of(system)
        if role == "review":
            self.review_calls += 1
            return self.review_response
        if role == "summary":
            self.summary_calls += 1
            return self.summary_response
        self.skeptic_calls += 1
        return self.skeptic_response

    def estimate_cost(self, system, user, expected_completion_tokens=800):
        return CostEstimate(
            model="fake", prompt_tokens=len(user) // 4,
            estimated_completion_tokens=expected_completion_tokens, estimated_cost_usd=self._cost,
        )


@pytest.fixture
def multi_file_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "invoice.py").write_text("def calculate_discount(price):\n    return price\n")
    (tmp_path / "checkout.py").write_text("def checkout(price):\n    return price\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    (tmp_path / "invoice.py").write_text(
        "def calculate_discount(price):\n    return price * 1.5\n"
    )
    (tmp_path / "checkout.py").write_text(
        "def checkout(price):\n    return price - 999\n"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "head")

    return tmp_path, base_sha


class MultiFileFakeLlm:
    """Scripts a different review response per file (by matching `+++ b/path`
    in the prompt) and always accepts in the skeptic role — this is what
    proves findings from every file in a multi-file PR survive a single
    `run_review` call, not just the first file reviewed.
    """

    def __init__(self, findings_by_file: dict[str, dict]):
        self._findings_by_file = findings_by_file
        self.review_calls = 0

    def complete_json(self, system, user):
        role = _role_of(system)
        if role == "review":
            self.review_calls += 1
            for path, finding in self._findings_by_file.items():
                if f"+++ b/{path}" in user:
                    return {"findings": [finding]}
            return {"findings": []}
        if role == "summary":
            return {"summary": "Two findings across two files."}
        return {"is_real": True, "is_actionable": True, "confidence": 0.95}

    def estimate_cost(self, system, user, expected_completion_tokens=800):
        return CostEstimate(
            model="fake", prompt_tokens=len(user) // 4,
            estimated_completion_tokens=expected_completion_tokens, estimated_cost_usd=0.01,
        )


def test_findings_from_every_file_in_a_large_pr_survive_one_review(multi_file_repo):
    repo_path, base_sha = multi_file_repo
    llm = MultiFileFakeLlm({
        "invoice.py": {
            "file": "invoice.py", "line": 2, "category": "correctness", "severity": "high",
            "confidence": 0.9, "title": "Discount direction looks backwards",
            "quoted_code": "return price * 1.5",
        },
        "checkout.py": {
            "file": "checkout.py", "line": 2, "category": "correctness", "severity": "high",
            "confidence": 0.9, "title": "Hardcoded discount amount",
            "quoted_code": "return price - 999",
        },
    })
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)

    # Both files' findings present in ONE pass — not "find one, fix it,
    # re-review, find the other" across multiple rounds.
    assert {v.finding.file for v in outcome.findings} == {"invoice.py", "checkout.py"}
    assert llm.review_calls == 2  # one call per file, not one call for the whole PR


def test_batched_cost_estimate_sums_across_files_not_just_one(multi_file_repo):
    repo_path, base_sha = multi_file_repo
    llm = MultiFileFakeLlm({})  # no findings needed — only estimate_cost matters here
    outcome = run_review(repo_path, base=base_sha, head="HEAD", dry_run=True, llm=llm)

    # Each file contributes len(user)//4 prompt tokens per MultiFileFakeLlm's
    # estimate_cost; two files means strictly more than either alone would.
    single_file_estimate = llm.estimate_cost("sys", "a single file's worth of text")
    assert outcome.estimate.prompt_tokens > single_file_estimate.prompt_tokens


FINDING = {
    "file": "invoice.py",
    "line": 2,
    "category": "correctness",
    "severity": "high",
    "confidence": 0.9,
    "title": "Discount can exceed 100%",
    "quoted_code": "rate = sum(p.rate for p in promos)",
}


def test_no_changes_short_circuits_cleanly(tmp_path):
    # base == head: nothing to diff, no LLM call should even be attempted
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "only commit")

    llm = FakeLlm()
    outcome = run_review(tmp_path, base="HEAD", head="HEAD", llm=llm)
    assert outcome.message == "no changes to review"
    assert llm.calls == 0


def test_dry_run_never_calls_the_review_model(repo):
    repo_path, base_sha = repo
    llm = FakeLlm()
    outcome = run_review(repo_path, base=base_sha, head="HEAD", dry_run=True, llm=llm)
    assert outcome.message.startswith("dry run")
    assert outcome.estimate is not None
    assert llm.calls == 0


def test_cost_ceiling_blocks_the_review(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(cost=5.00)
    config = Config(max_cost_per_run=1.00)
    with pytest.raises(CostCeilingExceeded):
        run_review(repo_path, base=base_sha, head="HEAD", config=config, llm=llm)


def test_end_to_end_a_real_finding_survives_the_gate(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]})
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    assert len(outcome.findings) == 1
    assert outcome.findings[0].finding.title == "Discount can exceed 100%"


def test_skeptic_rejection_means_no_findings_survive(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(
        review_response={"findings": [FINDING]},
        skeptic_response={"is_real": False, "is_actionable": False, "confidence": 0.9},
    )
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    assert outcome.findings == []
    assert len(outcome.dropped) == 1


def test_a_finding_pointing_at_a_line_outside_the_diff_is_dropped(repo):
    # real quote, wrong line: the model copied a changed line correctly but
    # reported it as line 90, which no hunk covers. Before the gate checked
    # locations this posted as a verified finding and then failed to place
    # its comment, visible only as a warning in the Action's log.
    repo_path, base_sha = repo
    misplaced = {**FINDING, "line": 90}
    llm = FakeLlm(review_response={"findings": [misplaced]})
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    assert outcome.findings == []
    assert outcome.dropped[0].dropped_at == DropStage.BAD_LOCATION


def test_render_text_names_the_stage_that_dropped_a_finding(repo):
    repo_path, base_sha = repo
    misplaced = {**FINDING, "line": 90}
    llm = FakeLlm(review_response={"findings": [misplaced]})
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    assert "1 dropped (1 bad location)" in render_text(outcome)


def test_render_json_is_valid_json_with_expected_shape(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]})
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    payload = json.loads(render_json(outcome))
    assert payload["findings"][0]["file"] == "invoice.py"
    assert payload["dropped_count"] == 0


def test_render_text_mentions_the_finding(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]})
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    text = render_text(outcome)
    assert "Discount can exceed 100%" in text
    assert "invoice.py:2" in text


def test_render_text_handles_no_findings():
    outcome = ReviewOutcome(findings=[], dropped=[], estimate=None, cut=[])
    assert "No findings" in render_text(outcome)


def test_main_model_flag_overrides_config_end_to_end(repo, capsys):
    # Real main() entrypoint, real (unmocked) cost estimator — --dry-run
    # means no network call happens, so this is safe to run for real.
    from groundtruth.cli import main

    repo_path, base_sha = repo
    exit_code = main([
        "review", "--repo", str(repo_path), "--base", base_sha, "--head", "HEAD",
        "--model", "openai/gpt-4o-mini", "--dry-run", "--format", "json",
    ])
    assert exit_code == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["model"] == "openai/gpt-4o-mini"


# --- _windowed_hunk_text: the DD-031-class fix -----------------------------
#
# A finding's fingerprint must drift only when the code it's actually about
# changes — never because something unrelated changed elsewhere in the same
# file (a CI-managed version stamp a few lines away, say). That failure mode
# is exactly what a "whole file's added lines, shared across every finding
# in it" hunk-text recipe produces; these tests pin the fix.

_SOURCE_100_LINES = "\n".join(f"line{i}" for i in range(1, 101))


def test_windowed_hunk_text_includes_added_lines_within_the_window():
    text = _windowed_hunk_text(_SOURCE_100_LINES, added_lines={5, 6, 7}, line=6, window=2)
    assert "line5" in text and "line6" in text and "line7" in text


def test_windowed_hunk_text_excludes_added_lines_outside_the_window():
    text = _windowed_hunk_text(_SOURCE_100_LINES, added_lines={5, 90}, line=5, window=2)
    assert "line90" not in text


def test_windowed_hunk_text_ignores_a_distant_added_line_entirely():
    # The actual regression: an unrelated added line elsewhere in the file
    # must not change the text computed for a finding nowhere near it.
    near_only = _windowed_hunk_text(_SOURCE_100_LINES, added_lines={5}, line=5, window=2)
    with_distant_addition = _windowed_hunk_text(_SOURCE_100_LINES, added_lines={5, 90}, line=5, window=2)
    assert near_only == with_distant_addition


class StableFindingLlm:
    """Always proposes the same finding (same file/line/quote) regardless of
    which head commit is under review, and always accepts it in the skeptic
    role — isolates whether the FINGERPRINT stays stable across re-reviews,
    independent of anything the model itself does differently between runs.
    """

    def __init__(self, quoted_code: str):
        self._quoted_code = quoted_code

    def complete_json(self, system, user):
        if "findings" in system.lower() or "senior code reviewer" in system.lower():
            if self._quoted_code not in user:
                return {"findings": []}
            return {
                "findings": [{
                    "file": "manifest.py", "line": 1, "category": "security", "severity": "high",
                    "confidence": 0.9, "title": "Hardcoded Vault reference",
                    "quoted_code": self._quoted_code,
                }]
            }
        return {"is_real": True, "is_actionable": True, "confidence": 0.95}

    def estimate_cost(self, system, user, expected_completion_tokens=800):
        return CostEstimate(
            model="fake", prompt_tokens=len(user) // 4,
            estimated_completion_tokens=expected_completion_tokens, estimated_cost_usd=0.01,
        )


def test_fingerprint_survives_an_unrelated_line_changing_elsewhere_in_the_file(tmp_path):
    annotation_line = 'annotation = "vault-secret-ref"'

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "README.md").write_text("placeholder\n")  # give the repo a real base commit
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    def write_manifest(image_tag: str) -> None:
        (tmp_path / "manifest.py").write_text(
            f"{annotation_line}\n"
            "filler_line_2 = None\n"
            "filler_line_3 = None\n"
            f'image_tag = "{image_tag}"\n'
        )

    llm = StableFindingLlm(annotation_line)

    write_manifest("v1.0.0")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "head1")
    head1_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    outcome1 = run_review(tmp_path, base=base_sha, head=head1_sha, llm=llm)

    # A later, CI-triggered push: only the image tag changes. The Vault
    # annotation line is byte-identical to head1.
    write_manifest("v1.0.1")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "head2 - ci version bump")
    head2_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    outcome2 = run_review(tmp_path, base=base_sha, head=head2_sha, llm=llm)

    assert len(outcome1.findings) == 1
    assert len(outcome2.findings) == 1
    assert outcome1.findings[0].fingerprint == outcome2.findings[0].fingerprint


# --- cost accounting covers every paid phase ------------------------------

def test_the_ceiling_counts_the_skeptic_pass_not_just_the_review(repo):
    # a per-call cost under the ceiling on its own, over it once the skeptic
    # call for the candidate is counted too
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]}, cost=0.6)
    config = Config(max_cost_per_run=1.00)
    with pytest.raises(CostCeilingExceeded) as exc:
        run_review(repo_path, base=base_sha, head="HEAD", config=config, llm=llm)
    assert "verification pass" in str(exc.value)
    assert llm.skeptic_calls == 0  # stopped before spending on it


def test_the_reported_estimate_includes_every_phase(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]}, cost=0.01)
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    # review call + skeptic call + summary call, all priced at 0.01 by the fake
    assert outcome.estimate.estimated_cost_usd == pytest.approx(0.03)


def test_a_dry_run_says_its_estimate_is_partial(repo):
    repo_path, base_sha = repo
    outcome = run_review(repo_path, base=base_sha, head="HEAD", dry_run=True, llm=FakeLlm())
    assert "review pass only" in outcome.message


# --- stage 6, end to end --------------------------------------------------

def test_the_summary_reaches_the_outcome_and_the_json(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]},
                  summary_response={"summary": "A contract break in invoice.py."})
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    assert outcome.summary == "A contract break in invoice.py."
    assert llm.summary_calls == 1
    payload = json.loads(render_json(outcome))
    assert payload["summary"] == "A contract break in invoice.py."
    assert payload["fingerprints"] == [outcome.findings[0].fingerprint]


def test_summary_disabled_makes_no_call(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]})
    outcome = run_review(repo_path, base=base_sha, head="HEAD",
                         config=Config(summary=False), llm=llm)
    assert outcome.summary is None
    assert llm.summary_calls == 0


def test_no_findings_means_no_summary_call(repo):
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": []})
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=llm)
    assert llm.summary_calls == 0
    assert outcome.summary is None


# --- the review remembers what it already posted --------------------------

def test_a_fingerprint_from_an_earlier_push_is_not_posted_again(repo):
    repo_path, base_sha = repo
    first = run_review(repo_path, base=base_sha, head="HEAD",
                       llm=FakeLlm(review_response={"findings": [FINDING]}))
    already = {first.findings[0].fingerprint}

    second = run_review(repo_path, base=base_sha, head="HEAD",
                        llm=FakeLlm(review_response={"findings": [FINDING]}),
                        seen_fingerprints=already)
    assert second.findings == []
    assert second.dropped[0].dropped_at == DropStage.DEDUPE


# --- a review that did not run must never read as a clean diff ------------

class BrokenLlm(FakeLlm):
    """Every review call raises; the skeptic and estimates still work."""

    def complete_json(self, system, user):
        if _role_of(system) == "review":
            self.review_calls += 1
            raise RuntimeError("provider unavailable")
        return super().complete_json(system, user)


def test_a_review_whose_calls_all_failed_is_reported_as_incomplete(repo):
    repo_path, base_sha = repo
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=BrokenLlm())

    assert outcome.findings == []
    assert outcome.review_incomplete is True
    assert outcome.review_failures == outcome.review_calls > 0


def test_the_text_output_says_the_review_did_not_run(repo):
    repo_path, base_sha = repo
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=BrokenLlm())
    text = render_text(outcome)

    assert "did not run" in text
    # the exact sentence that used to be a lie
    assert "No findings survived the quality gate." not in text


def test_the_json_carries_the_incomplete_flag_for_adapters(repo):
    repo_path, base_sha = repo
    outcome = run_review(repo_path, base=base_sha, head="HEAD", llm=BrokenLlm())
    payload = json.loads(render_json(outcome))

    assert payload["review_incomplete"] is True
    assert payload["review_failures"] == payload["review_calls"] > 0


def test_a_working_review_is_not_flagged_incomplete(repo):
    repo_path, base_sha = repo
    outcome = run_review(repo_path, base=base_sha, head="HEAD",
                         llm=FakeLlm(review_response={"findings": [FINDING]}))

    assert outcome.review_incomplete is False
    assert outcome.review_failures == 0
    assert "WARNING" not in render_text(outcome)


def test_findings_dropped_by_a_failed_verify_call_are_counted_separately(repo):
    # dropped because the checker broke, not because the finding was judged
    # wrong -- the reader needs to be able to tell those apart
    class BrokenSkeptic(FakeLlm):
        def complete_json(self, system, user):
            if _role_of(system) == "skeptic":
                raise RuntimeError("provider unavailable")
            return super().complete_json(system, user)

    repo_path, base_sha = repo
    outcome = run_review(repo_path, base=base_sha, head="HEAD",
                         llm=BrokenSkeptic(review_response={"findings": [FINDING]}))

    assert outcome.findings == []
    assert outcome.verify_failures == 1
    assert "verification call failed" in render_text(outcome)


# --- the summary must never cost findings that already passed -------------

def test_a_summary_over_the_ceiling_is_skipped_not_fatal(repo):
    # the cheapest call in the run must not be able to discard the findings
    # the expensive calls already produced and verified
    repo_path, base_sha = repo
    llm = FakeLlm(review_response={"findings": [FINDING]}, cost=0.4)
    config = Config(max_cost_per_run=1.00)

    outcome = run_review(repo_path, base=base_sha, head="HEAD", config=config, llm=llm)

    assert len(outcome.findings) == 1          # the work survives
    assert outcome.summary is None             # only the summary is given up
    assert llm.summary_calls == 0
    assert "summary skipped" in outcome.message


def test_each_role_is_built_with_its_own_model(repo, monkeypatch):
    # a cross-provider config only means something if the verifier really is
    # a separate client; this fails if every role ever collapses onto `model`
    import groundtruth.cli as cli_module

    built = []

    class RecordingClient(FakeLlm):
        def __init__(self, model, api_base=None, **kwargs):
            super().__init__(review_response={"findings": [FINDING]})
            built.append(model)

    monkeypatch.setattr(cli_module, "LlmClient", RecordingClient)
    repo_path, base_sha = repo
    config = Config(model="anthropic/claude-sonnet-5", verify_model="openai/gpt-4o-mini")
    run_review(repo_path, base=base_sha, head="HEAD", config=config)

    assert built[0] == "anthropic/claude-sonnet-5"   # review
    assert built[1] == "openai/gpt-4o-mini"          # verify
    assert built[2] == "openai/gpt-4o-mini"          # summary follows verify


def test_each_client_is_built_with_the_endpoint_for_its_own_model(repo, monkeypatch):
    # the pairing that matters: a model must never be sent to another
    # provider's endpoint
    import groundtruth.cli as cli_module

    built = []

    class RecordingClient(FakeLlm):
        def __init__(self, model, api_base=None, **kwargs):
            super().__init__(review_response={"findings": [FINDING]})
            built.append((model, api_base))

    monkeypatch.setattr(cli_module, "LlmClient", RecordingClient)
    repo_path, base_sha = repo
    config = Config(model="anthropic/claude-sonnet-5",
                    verify_model="nvidia_nim/moonshotai/kimi-k3",
                    verify_base_url="https://integrate.api.nvidia.com/v1")
    run_review(repo_path, base=base_sha, head="HEAD", config=config)

    nvidia = "https://integrate.api.nvidia.com/v1"
    assert built[0] == ("anthropic/claude-sonnet-5", None)
    assert built[1] == ("nvidia_nim/moonshotai/kimi-k3", nvidia)
    assert built[2] == ("nvidia_nim/moonshotai/kimi-k3", nvidia)


def test_a_cli_flag_overrides_the_environment(monkeypatch):
    from groundtruth.cli import _apply_endpoint_flags, build_arg_parser

    monkeypatch.setenv("GROUNDTRUTH_BASE_URL", "https://from-env")
    args = build_arg_parser().parse_args(["review", "--base", "main", "--base-url", "https://from-flag/"])
    config = _apply_endpoint_flags(load_config_for_test(), args)
    assert config.review_base_url == "https://from-flag"


def load_config_for_test():
    from groundtruth.config import load_config
    return load_config(None)


def test_each_client_gets_the_settings_for_its_own_model(repo, monkeypatch):
    import groundtruth.cli as cli_module

    built = []

    class RecordingClient(FakeLlm):
        def __init__(self, model, api_base=None, params=None, **kwargs):
            super().__init__(review_response={"findings": [FINDING]})
            built.append((model, params))

    monkeypatch.setattr(cli_module, "LlmClient", RecordingClient)
    repo_path, base_sha = repo
    config = Config(model="nvidia_nim/moonshotai/kimi-k3",
                    model_params={"temperature": 1.0, "max_tokens": 16384},
                    verify_model="nvidia_nim/z-ai/glm-5.3-flash",
                    verify_model_params={"max_tokens": 2048})
    run_review(repo_path, base=base_sha, head="HEAD", config=config)

    assert built[0] == ("nvidia_nim/moonshotai/kimi-k3", {"temperature": 1.0, "max_tokens": 16384})
    assert built[1] == ("nvidia_nim/z-ai/glm-5.3-flash", {"max_tokens": 2048})
    assert built[2] == ("nvidia_nim/z-ai/glm-5.3-flash", {"max_tokens": 2048})
