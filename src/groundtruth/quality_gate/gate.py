"""The quality gate: nothing reaches a pull request without clearing all of it.

Order matters and is deliberate:
  1. Hallucination check — free, no LLM call, kills the fabricated ones first
     so the (comparatively expensive) skeptic pass never wastes a call on a
     quote that doesn't even exist.
  2. Location check — also free: the quote is real, but is the line number
     attached to it inside a changed hunk? Runs before the fingerprint is
     used, because a bogus line produces a bogus fingerprint.
  3. Dedupe — a fingerprint lookup against everything already posted on this
     PR. Also free. No point cross-examining a finding that's already live.
  4. Skeptic cross-examination + confidence multiplication.
  5. Rank and cap — survivors sorted by severity-weighted combined
     confidence, truncated to `max_findings`; nothing here rejects a
     finding, it just decides posting order and where the cutoff falls.

Every dropped finding is recorded with which stage killed it and why — that
record is what the eval harness (next up on the roadmap) grades against, and
it's what turns "the bot seems okay" into a measurable, improvable number.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .fingerprint import fingerprint as compute_fingerprint
from .hallucination import quote_exists
from .models import DropStage, Finding, GateVerdict
from .skeptic import JsonLlm, combined_confidence, cross_examine


def _evidence_texts_to_str(evidence_texts: list[str]) -> str:
    return "\n---\n".join(evidence_texts)


@dataclass
class GateReport:
    posted: list[GateVerdict] = field(default_factory=list)
    dropped: list[GateVerdict] = field(default_factory=list)

    @property
    def all(self) -> list[GateVerdict]:
        return self.posted + self.dropped


def run_gate(
    findings: list[Finding],
    evidence_texts: list[str],
    hunk_text_for: Callable[[Finding], str],
    llm: JsonLlm,
    seen_fingerprints: set[str] | None = None,
    min_confidence: float = 0.7,
    max_findings: int = 10,
    line_is_changed: Callable[[Finding], bool] | None = None,
) -> GateReport:
    """Run every candidate finding through the full gate.

    `evidence_texts` is everything the reviewer was shown (diff + assembled
    context) — what the hallucination check searches. `hunk_text_for` maps a
    Finding to the text its fingerprint hashes — deliberately a callable
    rather than a `{file: text}` dict, and deliberately called **per
    finding**, not once per file: a fingerprint built from every added line
    in the whole file drifts whenever *anything* in that file changes, not
    just the code the finding is actually about, which produces duplicate
    comments and false "resolved" replies on findings nobody touched. The
    caller (`cli.py`) is the one that knows how to scope that text to a
    window around the finding's own line; this module only needs "give me
    the text for this finding," not how that text gets built.
    `seen_fingerprints` is the caller's memory of what's already posted on
    this PR (a set today; a database lookup in a real deployment) — findings
    are checked against a running copy that grows as this call proceeds, so
    two near-duplicate findings in the same batch also collapse correctly.
    `line_is_changed` answers "is this finding's line inside a changed hunk
    of its file?" — a callable for the same reason as `hunk_text_for`, so
    this module needs no diff parsing of its own. Left unset the check is
    skipped, which is what a caller holding findings with no diff behind
    them (a library user verifying claims against arbitrary evidence) wants.
    """
    seen = set(seen_fingerprints or set())
    report = GateReport()

    for finding in findings:
        hunk_text = hunk_text_for(finding)
        fp = compute_fingerprint(finding.file, hunk_text, finding.category, finding.quoted_code)

        if not quote_exists(finding.quoted_code, evidence_texts):
            report.dropped.append(
                GateVerdict(
                    finding=finding,
                    posted=False,
                    fingerprint=fp,
                    dropped_at=DropStage.HALLUCINATION,
                    reason="quoted_code was not found in the diff or assembled context",
                )
            )
            continue

        if line_is_changed is not None and not line_is_changed(finding):
            report.dropped.append(
                GateVerdict(
                    finding=finding,
                    posted=False,
                    fingerprint=fp,
                    dropped_at=DropStage.BAD_LOCATION,
                    reason=(
                        f"{finding.file}:{finding.line} is not inside a changed hunk — the quote is "
                        "real but its reported location is not, so a comment there could not be placed"
                    ),
                )
            )
            continue

        if fp in seen:
            report.dropped.append(
                GateVerdict(
                    finding=finding,
                    posted=False,
                    fingerprint=fp,
                    dropped_at=DropStage.DEDUPE,
                    reason="already posted on this PR (same file, same code, same category)",
                )
            )
            continue

        verdict = cross_examine(finding, _evidence_texts_to_str(evidence_texts), llm)
        combined = combined_confidence(finding.confidence, verdict)

        if combined < min_confidence:
            report.dropped.append(
                GateVerdict(
                    finding=finding,
                    posted=False,
                    fingerprint=fp,
                    combined_confidence=combined,
                    dropped_at=DropStage.LOW_CONFIDENCE,
                    reason=verdict.reason or f"combined confidence {combined:.2f} below {min_confidence}",
                )
            )
            continue

        seen.add(fp)
        report.posted.append(
            GateVerdict(finding=finding, posted=True, fingerprint=fp, combined_confidence=combined)
        )

    report.posted.sort(key=lambda v: v.score, reverse=True)
    overflow = report.posted[max_findings:]
    report.posted = report.posted[:max_findings]
    for verdict in overflow:
        report.dropped.append(
            GateVerdict(
                finding=verdict.finding,
                posted=False,
                fingerprint=verdict.fingerprint,
                combined_confidence=verdict.combined_confidence,
                reason=f"ranked below the top {max_findings} findings for this review",
            )
        )

    return report
