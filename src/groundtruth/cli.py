"""`groundtruth review` — the one command every front door calls.

A GitHub Action, a GitLab CI job, a Bitbucket Pipelines step, or the
self-hosted server's worker all end up running this same command; the only
thing that differs between them is how they got a diff and where they send
the JSON this prints. That's deliberate — see the project README's "one core
command, called the same way no matter who's asking."
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Config, ConfigError, load_config
from .context_engine import ContextBlock, DiffMap, build_context, parse_diff, render_file_diff
from .git_source import GitError, git_diff, load_sources
from .llm import CostEstimate, LlmClient
from .quality_gate import (
    SKEPTIC_CALL_FAILED,
    GateVerdict,
    line_in_changed_hunk,
    run_gate,
    skeptic_prompts,
)
from .reviewer import CallTally, propose_findings_for_diffmap, system_prompt
from .summary import summary_prompts, write_summary


class CostCeilingExceeded(RuntimeError):
    pass


@dataclass
class ReviewOutcome:
    findings: list[GateVerdict]
    dropped: list[GateVerdict]
    estimate: CostEstimate | None
    cut: list[str]
    message: str = ""
    summary: str | None = None
    review_calls: int = 0
    review_failures: int = 0
    verify_failures: int = 0

    @property
    def review_incomplete(self) -> bool:
        """True when the model never answered, so silence means nothing.

        A review that made no calls (a dry run, an empty diff) is not
        incomplete; a review whose calls all failed is, and the difference
        has to reach the reader.
        """
        return self.review_calls > 0 and self.review_failures == self.review_calls


def _add_estimates(*estimates: CostEstimate | None) -> CostEstimate | None:
    """Sum what every phase of one run is expected to cost.

    The ceiling used to be checked against the review pass alone, which is
    the largest phase but not the whole bill: the skeptic pass adds a call
    per surviving candidate and the summary adds one more. An estimate that
    leaves those out cannot honestly be called a ceiling.

    A phase whose model is missing from LiteLLM's price registry (a local
    Ollama model, say) makes the *total* unknown rather than smaller — a
    partial sum reported as a total is the failure worth avoiding here.
    """
    present = [e for e in estimates if e is not None]
    if not present:
        return None

    cost_known = all(e.estimated_cost_usd is not None for e in present)
    return CostEstimate(
        model=present[0].model,
        prompt_tokens=sum(e.prompt_tokens for e in present),
        estimated_completion_tokens=sum(e.estimated_completion_tokens for e in present),
        estimated_cost_usd=(
            round(sum(e.estimated_cost_usd for e in present), 6) if cost_known else None
        ),
    )


def _over_ceiling(estimate: CostEstimate | None, max_cost: float | None) -> bool:
    if max_cost is None or estimate is None or estimate.estimated_cost_usd is None:
        return False
    return estimate.estimated_cost_usd > max_cost


def _check_ceiling(estimate: CostEstimate | None, max_cost: float | None, phase: str) -> None:
    """Checked before each paid phase rather than once up front, because the
    number of skeptic calls is not known until the review pass has returned
    its candidates. Stopping between phases keeps the guarantee real without
    pretending the total was knowable earlier than it was.
    """
    if not _over_ceiling(estimate, max_cost):
        return
    raise CostCeilingExceeded(
        f"estimated cost ${estimate.estimated_cost_usd:.4f} through the {phase} exceeds "
        f"max_cost_per_run (${max_cost:.4f}) in config. Raise the ceiling, shrink the diff, "
        f"or re-run with --dry-run to inspect the estimate without this check."
    )


_FINGERPRINT_WINDOW = 2  # lines either side of a finding's own line


def _windowed_hunk_text(
    source: str, added_lines: set[int], line: int, window: int = _FINGERPRINT_WINDOW
) -> str:
    """Only the added lines within `window` of `line` — not every added line
    in the whole file. A finding's fingerprint should drift only when the
    code around *it* changes, never because something unrelated changed
    elsewhere in the same file (a CI-managed version stamp a few lines away,
    say). Scoping this per finding instead of once per file is the fix for
    exactly that failure mode.
    """
    lines = source.splitlines()
    lo, hi = line - window, line + window
    selected = [lines[i - 1] for i in sorted(added_lines) if lo <= i <= hi and 1 <= i <= len(lines)]
    return "\n".join(selected)


def _make_line_is_changed(diffmap: DiffMap):
    """Builds the per-finding location check `run_gate` uses — the gate asks
    "is this line inside a changed hunk?", and only the caller holds the
    parsed diff that can answer it. A file the diff never mentioned has no
    spans, so every finding against it fails the check.
    """
    spans = {
        path: [(hunk.new_start, hunk.new_end) for hunk in file_diff.hunks]
        for path, file_diff in diffmap.items()
    }

    def line_is_changed(finding) -> bool:
        return line_in_changed_hunk(finding.line, spans.get(finding.file, ()))

    return line_is_changed


def _make_hunk_text_for(head_sources: dict[str, str], diffmap: DiffMap):
    """Builds the per-finding callable `run_gate` uses to compute fingerprint
    input — `quality_gate` shouldn't need to know about added-line windows
    or how to read a file's source, only "give me the text for this finding."
    """

    def hunk_text_for(finding) -> str:
        source = head_sources.get(finding.file, "")
        added = diffmap[finding.file].added if finding.file in diffmap else set()
        return _windowed_hunk_text(source, added, finding.line)

    return hunk_text_for


def _estimate_batched_cost(
    diffmap: DiffMap,
    context_blocks: list[ContextBlock],
    llm: LlmClient,
    dimensions: list[str] | None,
) -> CostEstimate:
    """The review pass now makes one call per changed file (see
    `reviewer.propose_findings_for_diffmap` for why), so the estimate has to
    sum one per-file call each — a single whole-diff estimate would silently
    undercount real spend on any PR touching more than one file.
    """
    system = system_prompt(dimensions)
    context_text = "".join(block.text for block in context_blocks)
    total_prompt_tokens = 0
    total_cost = 0.0
    cost_known = True
    files_counted = 0
    model_name = ""

    for path, file_diff in diffmap.items():
        if not file_diff.hunks:
            continue
        files_counted += 1
        file_text = render_file_diff(path, file_diff)
        per_file = llm.estimate_cost(system=system, user=file_text + context_text)
        model_name = per_file.model
        total_prompt_tokens += per_file.prompt_tokens
        if per_file.estimated_cost_usd is None:
            cost_known = False
        else:
            total_cost += per_file.estimated_cost_usd

    return CostEstimate(
        model=model_name,
        prompt_tokens=total_prompt_tokens,
        estimated_completion_tokens=800 * files_counted,
        estimated_cost_usd=total_cost if cost_known else None,
    )


def _estimate_skeptic_cost(
    candidates: list, evidence_texts: list[str], llm: LlmClient
) -> CostEstimate | None:
    """One skeptic call per candidate, priced with the prompt that will
    actually be sent. Candidates dropped by the free checks never reach the
    model, so this is an upper bound — the right side to err on for a
    ceiling.
    """
    if not candidates:
        return None
    evidence = "\n---\n".join(evidence_texts)
    estimates = []
    for finding in candidates:
        system, user = skeptic_prompts(finding, evidence)
        estimates.append(llm.estimate_cost(system=system, user=user, expected_completion_tokens=200))
    return _add_estimates(*estimates)


def _estimate_summary_cost(posted: list[GateVerdict], llm: LlmClient) -> CostEstimate | None:
    if not posted:
        return None
    system, user = summary_prompts(posted)
    return llm.estimate_cost(system=system, user=user, expected_completion_tokens=150)


def _resolve_diff_text(repo: Path, base: str, head: str, diff_path: Path | None) -> str:
    if diff_path is None:
        return git_diff(repo, base, head)
    if str(diff_path) == "-":
        return sys.stdin.read()
    return Path(diff_path).read_text()


def run_review(
    repo: Path,
    base: str,
    head: str = "HEAD",
    diff_path: Path | None = None,
    config: Config | None = None,
    dry_run: bool = False,
    llm: LlmClient | None = None,
    seen_fingerprints: set[str] | None = None,
) -> ReviewOutcome:
    """`llm` is injectable so callers (tests, mainly) can supply a fake
    instead of hitting a real provider — production callers (the CLI's own
    `main()`) leave it unset and get a real `LlmClient` built from `config`.
    """
    config = config or Config()
    diff_text = _resolve_diff_text(repo, base, head, diff_path)
    diffmap = parse_diff(diff_text)

    if not diffmap:
        return ReviewOutcome(findings=[], dropped=[], estimate=None, cut=[], message="no changes to review")

    paths = list(diffmap.keys())
    head_sources = load_sources(repo, head, paths)
    base_sources = load_sources(repo, base, paths)

    ctx = build_context(
        repo_root=repo,
        diffmap=diffmap,
        head_sources=head_sources,
        base_sources=base_sources,
        budget_tokens=config.context_token_budget,
    )

    # One injected client stands in for every role (tests, mainly). Left
    # unset, each role gets its own: the tiers are config, and unset tier
    # settings resolve back to the one `model` the user named.
    review_llm = llm or LlmClient(model=config.review_model, api_base=config.llm_base_url)
    verify_llm = llm or LlmClient(model=config.resolved_verify_model, api_base=config.llm_base_url)
    summary_llm = llm or LlmClient(model=config.resolved_summary_model, api_base=config.llm_base_url)

    review_estimate = _estimate_batched_cost(diffmap, ctx.blocks, review_llm, config.dimensions)

    if dry_run:
        return ReviewOutcome(
            findings=[], dropped=[], estimate=review_estimate, cut=ctx.cut,
            message="dry run — no LLM review call made (estimate covers the review pass only)",
        )

    _check_ceiling(review_estimate, config.max_cost_per_run, "review pass")

    tally = CallTally()
    candidates = propose_findings_for_diffmap(
        diffmap, ctx.blocks, review_llm,
        dimensions=config.dimensions,
        max_diff_tokens_per_call=config.max_diff_tokens_per_call,
        tally=tally,
    )

    evidence_texts = [diff_text] + [block.text for block in ctx.blocks]

    skeptic_estimate = _estimate_skeptic_cost(candidates, evidence_texts, verify_llm)
    _check_ceiling(
        _add_estimates(review_estimate, skeptic_estimate),
        config.max_cost_per_run,
        "verification pass",
    )

    report = run_gate(
        candidates,
        evidence_texts,
        _make_hunk_text_for(head_sources, diffmap),
        verify_llm,
        seen_fingerprints=seen_fingerprints,
        min_confidence=config.min_confidence,
        max_findings=config.max_inline_comments,
        line_is_changed=_make_line_is_changed(diffmap),
    )

    summary_estimate = None
    summary_text = None
    skipped_summary = False
    if config.summary and report.posted:
        summary_estimate = _estimate_summary_cost(report.posted, summary_llm)
        projected = _add_estimates(review_estimate, skeptic_estimate, summary_estimate)

        # Deliberately a skip, not the exception the earlier phases raise.
        # By this point the findings are verified and already paid for, and
        # raising here would throw all of them away over the cheapest call
        # in the run -- losing the work to save a fraction of its cost.
        if _over_ceiling(projected, config.max_cost_per_run):
            skipped_summary = True
            summary_estimate = None
        else:
            summary_text = write_summary(report.posted, summary_llm)

    verify_failures = sum(1 for v in report.dropped if v.reason == SKEPTIC_CALL_FAILED)

    message = ""
    if skipped_summary:
        message = "summary skipped: it would have crossed max_cost_per_run. Findings are unaffected."

    return ReviewOutcome(
        findings=report.posted,
        dropped=report.dropped,
        estimate=_add_estimates(review_estimate, skeptic_estimate, summary_estimate),
        cut=ctx.cut,
        summary=summary_text,
        message=message,
        review_calls=tally.made,
        review_failures=tally.failed,
        verify_failures=verify_failures,
    )


def _verdict_to_dict(verdict: GateVerdict) -> dict:
    f = verdict.finding
    return {
        "file": f.file,
        "line": f.line,
        "category": f.category,
        "severity": f.severity.value,
        "title": f.title,
        "quoted_code": f.quoted_code,
        "confidence": verdict.combined_confidence,
        "fingerprint": verdict.fingerprint,
    }


def render_json(outcome: ReviewOutcome) -> str:
    payload = {
        "findings": [_verdict_to_dict(v) for v in outcome.findings],
        "summary": outcome.summary,
        "fingerprints": [v.fingerprint for v in outcome.findings],
        "dropped_count": len(outcome.dropped),
        "review_incomplete": outcome.review_incomplete,
        "review_calls": outcome.review_calls,
        "review_failures": outcome.review_failures,
        "verify_failures": outcome.verify_failures,
        "model": outcome.estimate.model if outcome.estimate else None,
        "estimated_cost_usd": outcome.estimate.estimated_cost_usd if outcome.estimate else None,
        "context_cut": outcome.cut,
        "message": outcome.message,
    }
    return json.dumps(payload, indent=2)


def _format_cost(estimate: CostEstimate) -> str:
    if estimate.estimated_cost_usd is None:
        return "unknown (model not in the LiteLLM cost registry)"
    return f"${estimate.estimated_cost_usd:.4f}"


def _dropped_breakdown(dropped: list[GateVerdict]) -> str:
    """"2 low confidence, 1 bad location" — which check rejected what, in
    one line. A bare count says the gate did something; naming the stages
    says whether the reviewer or the locations are the thing to look at.
    """
    counts = Counter(
        v.dropped_at.value if v.dropped_at else "ranked out" for v in dropped
    )
    parts = [f"{n} {stage.replace('_', ' ')}" for stage, n in counts.most_common()]
    return ", ".join(parts)


def render_text(outcome: ReviewOutcome) -> str:
    """The human-facing output. Findings come pre-sorted by the gate, and
    each one is a three-line block — headline, then its metadata, then the
    code it is about — with the columns aligned so a run of findings can be
    scanned down rather than read across.
    """
    lines: list[str] = []

    if outcome.message:
        lines.append(outcome.message)

    if outcome.estimate:
        lines.append(
            f"model {outcome.estimate.model}  ·  estimated cost {_format_cost(outcome.estimate)}"
            f"  ·  {outcome.estimate.prompt_tokens:,} prompt tokens"
        )

    if outcome.review_incomplete:
        lines.extend([
            "",
            f"WARNING: all {outcome.review_calls} review call(s) failed. "
            "This is not a clean diff -- the review did not run. "
            "Check the provider key, the model name and the provider's status.",
        ])
    elif outcome.review_failures:
        lines.extend([
            "",
            f"WARNING: {outcome.review_failures} of {outcome.review_calls} review call(s) failed, "
            "so some files were not reviewed.",
        ])

    if outcome.verify_failures:
        lines.extend([
            "",
            f"WARNING: {outcome.verify_failures} finding(s) were dropped because the verification "
            "call failed, not because they were judged wrong.",
        ])

    if not outcome.findings:
        if lines:
            lines.append("")
        if outcome.review_incomplete:
            lines.append("No findings reported, because the review did not complete.")
        elif outcome.dropped:
            # the case where naming the stage matters most: everything was
            # rejected, and "no findings" alone doesn't say whether that is
            # a clean diff, a noisy reviewer, or misreported locations
            lines.append(
                f"No findings survived the quality gate — "
                f"{len(outcome.dropped)} dropped ({_dropped_breakdown(outcome.dropped)})."
            )
        else:
            lines.append("No findings survived the quality gate.")
    else:
        if outcome.summary:
            lines.extend(["", outcome.summary])

        headline = f"{len(outcome.findings)} verified finding(s)"
        if outcome.dropped:
            headline += f", {len(outcome.dropped)} dropped ({_dropped_breakdown(outcome.dropped)})"
        lines.extend(["", headline, ""])

        sev_width = max(len(v.finding.severity.value) for v in outcome.findings)
        loc_width = max(len(f"{v.finding.file}:{v.finding.line}") for v in outcome.findings)
        indent = " " * (sev_width + loc_width + 6)

        for verdict in outcome.findings:
            f = verdict.finding
            severity = f.severity.value.upper().ljust(sev_width)
            location = f"{f.file}:{f.line}".ljust(loc_width)
            confidence = verdict.combined_confidence or 0.0
            lines.append(f"  {severity}  {location}  {f.title}")
            lines.append(f"{indent}{f.category} · confidence {confidence:.2f}")
            lines.append(f"{indent}{f.quoted_code.strip()}")
            lines.append("")

    if outcome.cut:
        lines.append(f"context trimmed to fit the budget: {len(outcome.cut)} block(s)")

    return "\n".join(lines).rstrip() + "\n"


def _run_eval(args) -> int:
    """`groundtruth eval` — the measurement CI gates on.

    Exits non-zero when a threshold is missed, so a prompt or model change
    that quietly loses bug catches fails the build instead of being noticed
    months later.
    """
    from .eval import load_cases, render_report, report_to_dict, run_suite

    cases = load_cases(args.cases)
    if not cases:
        print(f"error: no cases found in {args.cases}", file=sys.stderr)
        return 1

    clients: dict = {}
    if args.live:
        # Offline, every case replays a recorded answer, so a "catch" only
        # proves the pipeline let a known-good finding through. Live, the
        # recorded answers are thrown away and your configured models have
        # to find the bug themselves -- which is the only thing that
        # measures review quality rather than plumbing.
        config = load_config(args.config)
        if args.model:
            config = replace(config, model=args.model)
        if args.verify_model:
            config = replace(config, verify_model=args.verify_model)
        clients = {
            "review_llm": LlmClient(model=config.review_model, api_base=config.llm_base_url),
            "verify_llm": LlmClient(model=config.resolved_verify_model, api_base=config.llm_base_url),
        }
        # pipeline cases are built around a specific recorded mistake a live
        # model will not reproduce, so they only make sense offline
        cases = [c for c in cases if c.track == "recall"]
        print(
            f"live eval: {len(cases)} recall case(s), review={config.review_model}, "
            f"verify={config.resolved_verify_model} -- this makes real, billed model calls",
            file=sys.stderr,
        )

    report = run_suite(cases, min_confidence=args.min_confidence, **clients)
    print(json.dumps(report_to_dict(report), indent=2) if args.format == "json" else render_report(report))

    failures = []
    # A run where the model never answered has not measured anything, so it
    # fails rather than reporting a catch rate that means "no calls worked".
    if report.review_failures_total:
        failures.append(
            f"{report.review_failures_total} of {report.review_calls_total} review call(s) failed "
            "-- the catch rate does not measure the model"
        )
    # A leak is a gate regression: a case asserted this finding must be
    # rejected and it reached the pull request instead. There is no
    # threshold to tune -- the case says it must not get through.
    if report.gate_unexercised_total and not args.live:
        # offline, every gate case replays a recorded proposal, so a finding
        # that was never proposed means the case itself is malformed
        failures.append(
            f"{report.gate_unexercised_total} gate assertion(s) were never exercised -- "
            "the recorded review does not propose them"
        )
    if report.gate_wrong_stage_total:
        failures.append(
            f"{report.gate_wrong_stage_total} gate assertion(s) were rejected by a different stage "
            "than the case requires -- the right outcome for the wrong reason"
        )
    if report.gate_leaked_total:
        failures.append(
            f"{report.gate_leaked_total} finding(s) leaked through the gate that a case "
            "asserts must be rejected"
        )
    if args.min_catch is not None and report.catch_rate < args.min_catch:
        failures.append(f"catch rate {report.catch_rate:.0%} below --min-catch {args.min_catch:.0%}")
    if args.max_fp is not None and report.false_positive_rate > args.max_fp:
        failures.append(
            f"false positive rate {report.false_positive_rate:.2f} above --max-fp {args.max_fp:.2f}"
        )

    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    return 1 if failures else 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="groundtruth")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="Review a diff and report verified findings.")
    review.add_argument("--repo", type=Path, default=Path("."))
    review.add_argument("--base", required=True, help="Base ref (git) to diff against.")
    review.add_argument("--head", default="HEAD", help="Head ref (git); defaults to HEAD.")
    review.add_argument(
        "--diff", type=Path, default=None,
        help="Read the diff from this file (or '-' for stdin) instead of running `git diff`.",
    )
    review.add_argument(
        "--config", type=Path, default=None,
        help="Path to .groundtruth.yml (default: <repo>/.groundtruth.yml).",
    )
    review.add_argument(
        "--model", default=None,
        help="Override the model from .groundtruth.yml (e.g. anthropic/claude-sonnet-5).",
    )
    review.add_argument("--format", choices=["json", "text"], default="json")
    review.add_argument(
        "--dry-run", action="store_true",
        help="Estimate cost and exit without calling the review model.",
    )
    review.add_argument(
        "--seen-fingerprint", action="append", default=[], metavar="FP",
        help=(
            "A fingerprint already posted on this pull request; repeatable. "
            "Findings matching one are dropped as duplicates instead of being "
            "posted again. Adapters read these back from their own last comment."
        ),
    )
    review.add_argument(
        "--no-summary", action="store_true",
        help="Skip the summary call even when the config enables it.",
    )

    evaluate = sub.add_parser(
        "eval", help="Replay labeled cases through the pipeline and grade the result."
    )
    evaluate.add_argument(
        "--cases", type=Path, default=Path("cases"),
        help="A case file, or a directory of them (default: ./cases).",
    )
    evaluate.add_argument(
        "--min-catch", type=float, default=None, metavar="RATE",
        help="Fail if the catch rate falls below this (0-1), e.g. 0.8.",
    )
    evaluate.add_argument(
        "--max-fp", type=float, default=None, metavar="RATE",
        help="Fail if false positives per expected finding rise above this, e.g. 0.2.",
    )
    evaluate.add_argument(
        "--min-confidence", type=float, default=0.7,
        help="The gate threshold to replay against (default: 0.7).",
    )
    evaluate.add_argument("--format", choices=["json", "text"], default="text")
    evaluate.add_argument(
        "--live", action="store_true",
        help=(
            "Run the recall cases against real models instead of recorded answers. "
            "Needs provider keys and makes billed calls."
        ),
    )
    evaluate.add_argument(
        "--config", type=Path, default=Path(".groundtruth.yml"),
        help="Config to take models from in --live mode (default: ./.groundtruth.yml).",
    )
    evaluate.add_argument("--model", default=None, help="Override the review model for --live.")
    evaluate.add_argument(
        "--verify-model", default=None,
        help="Override the verify model for --live -- e.g. to compare a same-provider "
             "verifier against a cross-provider one on the same cases.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    # stderr, not stdout — stdout carries `--format json` for adapters to
    # parse, and a log line mixed into that output would break every one of
    # them. INFO is what review_call_prompt_size logs at; this is the whole
    # reason it's visible in a real run instead of only in tests, where
    # caplog turns logging on regardless of this call.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "review":
        config_path = args.config or (args.repo / ".groundtruth.yml")
        try:
            config = load_config(config_path)
            if args.model:
                config = replace(config, model=args.model)
            if args.no_summary:
                config = replace(config, summary=False)
            outcome = run_review(
                repo=args.repo,
                base=args.base,
                head=args.head,
                diff_path=args.diff,
                config=config,
                dry_run=args.dry_run,
                seen_fingerprints=set(args.seen_fingerprint) or None,
            )
        except (GitError, ConfigError, CostCeilingExceeded) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        output = render_json(outcome) if args.format == "json" else render_text(outcome)
        print(output)
        return 0

    if args.command == "eval":
        return _run_eval(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
