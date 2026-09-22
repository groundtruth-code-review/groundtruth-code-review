"""Replay labeled pull requests through the pipeline and grade the result.

A review tool that cannot be measured is a review tool nobody can improve:
every prompt edit, model swap and gate threshold is otherwise judged by
reading a diff and feeling good about it. This harness turns that into four
numbers per case.

Each expected finding lands in exactly one bucket:

  caught          the pipeline posted it
  gated           the reviewer proposed it and a gate stage dropped it
  missed          the reviewer never proposed it at all

and every posted finding matching nothing expected is a false positive.

The distinction between *gated* and *missed* is the point of the whole
exercise. Both look like "the bot said nothing", and they have opposite
fixes: a gated finding means the gate is too strict (or the finding quoted
its evidence badly), a missed one means the review prompt or the context
never gave the model what it needed. A single catch-rate number hides which
of the two just got worse.

Cases carry recorded model responses, so the default run is offline,
deterministic and free — what CI needs. Pass real clients instead and the
same cases measure a live model, which is how two providers get compared on
your own bugs rather than on a public benchmark.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..context_engine import build_context, parse_diff
from ..quality_gate import Finding, line_in_changed_hunk, run_gate
from ..reviewer import propose_findings_for_diffmap

_LINE_TOLERANCE = 2  # a model may point at a neighbouring line of the same change


@dataclass(frozen=True)
class ExpectedFinding:
    """One labeled bug in a case. `title_contains` is optional: for most
    cases "something at this location" is the claim being tested, and
    demanding particular wording from a model would fail the harness on
    rephrasing rather than on substance.
    """

    file: str
    line: int
    title_contains: str = ""

    def matches(self, finding: Finding) -> bool:
        if finding.file != self.file:
            return False
        if abs(finding.line - self.line) > _LINE_TOLERANCE:
            return False
        if self.title_contains and self.title_contains.lower() not in finding.title.lower():
            return False
        return True


def _expected(items: list) -> tuple[ExpectedFinding, ...]:
    return tuple(
        ExpectedFinding(
            file=item["file"],
            line=int(item["line"]),
            title_contains=item.get("title_contains", ""),
        )
        for item in items
    )


@dataclass(frozen=True)
class EvalCase:
    name: str
    diff: str
    head_sources: dict[str, str]
    base_sources: dict[str, str] = field(default_factory=dict)
    expected_findings: tuple[ExpectedFinding, ...] = ()
    # Findings the reviewer is expected to propose and the gate is expected
    # to reject. Kept apart from `expected_findings` on purpose: a case that
    # exists to prove the gate works can never be "caught", so counting it
    # in the catch rate caps that rate below 1.0 for structural reasons and
    # makes --min-catch unusable as a CI gate.
    expect_gated: tuple[ExpectedFinding, ...] = ()
    recorded_review: dict | None = None
    recorded_skeptic: dict | None = None

    @classmethod
    def from_dict(cls, data: dict, name: str = "") -> "EvalCase":
        recorded = data.get("recorded", {})
        return cls(
            name=data.get("name") or name,
            diff=data["diff"],
            head_sources=data.get("head_sources", {}),
            base_sources=data.get("base_sources", {}),
            expected_findings=_expected(data.get("expected_findings", [])),
            expect_gated=_expected(data.get("expect_gated", [])),
            recorded_review=recorded.get("review"),
            recorded_skeptic=recorded.get("skeptic"),
        )


@dataclass
class CaseResult:
    name: str
    caught: list[ExpectedFinding] = field(default_factory=list)
    gated: list[tuple[ExpectedFinding, str]] = field(default_factory=list)
    missed: list[ExpectedFinding] = field(default_factory=list)
    false_positives: list[Finding] = field(default_factory=list)
    # assertions that the gate rejected something it was supposed to reject
    gate_held: list[ExpectedFinding] = field(default_factory=list)
    gate_leaked: list[ExpectedFinding] = field(default_factory=list)

    @property
    def expected_total(self) -> int:
        return len(self.caught) + len(self.gated) + len(self.missed)


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def expected_total(self) -> int:
        return sum(case.expected_total for case in self.cases)

    @property
    def caught_total(self) -> int:
        return sum(len(case.caught) for case in self.cases)

    @property
    def gated_total(self) -> int:
        return sum(len(case.gated) for case in self.cases)

    @property
    def missed_total(self) -> int:
        return sum(len(case.missed) for case in self.cases)

    @property
    def false_positive_total(self) -> int:
        return sum(len(case.false_positives) for case in self.cases)

    @property
    def gate_held_total(self) -> int:
        return sum(len(case.gate_held) for case in self.cases)

    @property
    def gate_leaked_total(self) -> int:
        """Findings the gate was supposed to reject and did not. Any number
        above zero is a gate regression, which is why it is reported on its
        own rather than folded into the false-positive count.
        """
        return sum(len(case.gate_leaked) for case in self.cases)

    @property
    def catch_rate(self) -> float:
        """Caught over expected. A suite with nothing expected scores 1.0
        rather than dividing by zero — no claims made, none broken.
        """
        if self.expected_total == 0:
            return 1.0
        return self.caught_total / self.expected_total

    @property
    def false_positive_rate(self) -> float:
        """False positives per expected finding, which can exceed 1.0 on a
        noisy run. Deliberately not "share of posted findings": that ratio
        improves when the bot posts *more* correct findings, so it would let
        new noise hide behind unrelated wins.
        """
        if self.expected_total == 0:
            return float(self.false_positive_total)
        return self.false_positive_total / self.expected_total


class RecordedLlm:
    """Replays one recorded response, for one role, however many times that
    role is called. No network, no key, no variance between runs.
    """

    def __init__(self, response: dict | None):
        self._response = response or {}
        self.calls = 0

    def complete_json(self, system: str, user: str) -> dict:
        self.calls += 1
        return self._response


def load_cases(path: Path | str) -> list[EvalCase]:
    """Load one `.json` case file, or every case in a directory, sorted by
    filename so a report reads the same way twice.
    """
    path = Path(path)
    if not path.exists():
        # the caller reports "no cases found" with the path it was given; a
        # traceback from inside the loader would say less and read worse
        return []
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    cases = []
    for file in files:
        data = json.loads(file.read_text())
        cases.append(EvalCase.from_dict(data, name=file.stem))
    return cases


def run_case(case: EvalCase, review_llm=None, verify_llm=None, min_confidence: float = 0.7) -> CaseResult:
    """Run one case through the real context engine, reviewer and gate.

    The case's sources are written to a temporary directory because the
    context engine searches a working tree for callers — replaying against
    the real assembly step is the point, so faking it away would leave the
    part most likely to regress untested.
    """
    review_llm = review_llm or RecordedLlm(case.recorded_review)
    verify_llm = verify_llm or RecordedLlm(case.recorded_skeptic)

    diffmap = parse_diff(case.diff)
    result = CaseResult(name=case.name)

    with tempfile.TemporaryDirectory() as workdir:
        root = Path(workdir)
        for rel_path, text in case.head_sources.items():
            file = root / rel_path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)

        ctx = build_context(
            repo_root=root,
            diffmap=diffmap,
            head_sources=case.head_sources,
            base_sources=case.base_sources,
        )

        candidates = propose_findings_for_diffmap(diffmap, ctx.blocks, review_llm)
        evidence = [case.diff] + [block.text for block in ctx.blocks]

        spans = {
            path: [(hunk.new_start, hunk.new_end) for hunk in file_diff.hunks]
            for path, file_diff in diffmap.items()
        }
        report = run_gate(
            candidates,
            evidence,
            lambda finding: case.head_sources.get(finding.file, ""),
            verify_llm,
            min_confidence=min_confidence,
            line_is_changed=lambda f: line_in_changed_hunk(f.line, spans.get(f.file, ())),
        )

    posted = [verdict.finding for verdict in report.posted]
    matched_posted: set[int] = set()

    for expected in case.expected_findings:
        hit = next(
            (i for i, finding in enumerate(posted) if i not in matched_posted and expected.matches(finding)),
            None,
        )
        if hit is not None:
            matched_posted.add(hit)
            result.caught.append(expected)
            continue

        dropped = next(
            (v for v in report.dropped if expected.matches(v.finding)),
            None,
        )
        if dropped is not None:
            stage = dropped.dropped_at.value if dropped.dropped_at else "ranked out"
            result.gated.append((expected, stage))
            continue

        result.missed.append(expected)

    # Gate assertions: proposed, then correctly rejected. A leak here means
    # something the gate used to stop is now reaching pull requests.
    for expected in case.expect_gated:
        leaked = next(
            (i for i, finding in enumerate(posted) if i not in matched_posted and expected.matches(finding)),
            None,
        )
        if leaked is not None:
            matched_posted.add(leaked)
            result.gate_leaked.append(expected)
        else:
            result.gate_held.append(expected)

    result.false_positives = [
        finding for i, finding in enumerate(posted) if i not in matched_posted
    ]
    return result


def run_suite(cases: list[EvalCase], **kwargs) -> EvalReport:
    return EvalReport(cases=[run_case(case, **kwargs) for case in cases])


def render_report(report: EvalReport) -> str:
    lines = [
        f"{len(report.cases)} case(s), {report.expected_total} expected finding(s)",
        "",
    ]
    for case in report.cases:
        lines.append(f"  {case.name}")
        for expected in case.caught:
            lines.append(f"    caught   {expected.file}:{expected.line}")
        for expected, stage in case.gated:
            lines.append(f"    gated    {expected.file}:{expected.line}  ({stage})")
        for expected in case.missed:
            lines.append(f"    missed   {expected.file}:{expected.line}")
        for expected in case.gate_held:
            lines.append(f"    gate ok  {expected.file}:{expected.line}  (rejected, as required)")
        for expected in case.gate_leaked:
            lines.append(f"    LEAKED   {expected.file}:{expected.line}  (gate should have rejected this)")
        for finding in case.false_positives:
            lines.append(f"    false +  {finding.file}:{finding.line}  {finding.title}")
        lines.append("")

    lines.append(
        f"catch rate {report.catch_rate:.0%}  "
        f"({report.caught_total} caught, {report.gated_total} gated, {report.missed_total} missed)"
    )
    lines.append(
        f"false positives {report.false_positive_total} "
        f"({report.false_positive_rate:.2f} per expected finding)"
    )
    if report.gate_held_total or report.gate_leaked_total:
        lines.append(
            f"gate assertions {report.gate_held_total} held, {report.gate_leaked_total} leaked"
        )
    return "\n".join(lines)


def report_to_dict(report: EvalReport) -> dict:
    return {
        "cases": [
            {
                "name": case.name,
                "caught": [f"{e.file}:{e.line}" for e in case.caught],
                "gated": [{"finding": f"{e.file}:{e.line}", "stage": stage} for e, stage in case.gated],
                "missed": [f"{e.file}:{e.line}" for e in case.missed],
                "gate_held": [f"{e.file}:{e.line}" for e in case.gate_held],
                "gate_leaked": [f"{e.file}:{e.line}" for e in case.gate_leaked],
                "false_positives": [
                    {"file": f.file, "line": f.line, "title": f.title} for f in case.false_positives
                ],
            }
            for case in report.cases
        ],
        "expected_total": report.expected_total,
        "caught_total": report.caught_total,
        "gated_total": report.gated_total,
        "missed_total": report.missed_total,
        "false_positive_total": report.false_positive_total,
        "catch_rate": round(report.catch_rate, 4),
        "false_positive_rate": round(report.false_positive_rate, 4),
        "gate_held_total": report.gate_held_total,
        "gate_leaked_total": report.gate_leaked_total,
    }
