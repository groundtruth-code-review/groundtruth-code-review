"""Stage 6: one cheap call that turns verified findings into a summary.

This is the only text that reaches a pull request without having gone
through the gate itself, so the rule is narrow on purpose: the summarizer
may **group and rephrase findings that already passed**, and nothing else.
It is a presentation step, not a second opinion.

Two guards make that rule enforced rather than requested:

  1. Grounding — the summary may not name a file or a line that is not in
     the verified set. A summary that does is thrown away, exactly as the
     hallucination check throws away a finding quoting code that isn't
     there.
  2. Fail open — a broken call, a timeout, or a failed grounding check
     returns None, and the caller falls back to the deterministic list it
     would have posted anyway. A bad call costs wording, never the review.

The call lives here, in the core, rather than in a platform adapter: the
adapters speak one API each and share nothing, so a summary written in one
of them would have to be written again in every other.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from .quality_gate import GateVerdict

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = """\
You are writing the summary comment for a code review that has already been
completed and verified. You are NOT reviewing the code.

Write 1-3 short sentences for the pull request author that say what the
findings add up to. Group findings that share a cause ("three missing null
checks in the payments path") instead of restating each one; the individual
findings are already posted as their own inline comments.

Rules:
- Use ONLY the findings given below. Never add a problem, a file, a line
  number, or a recommendation that is not among them.
- Never soften or upgrade a finding's severity.
- No preamble, no sign-off, no markdown headings. Plain sentences.

Reply with JSON: {"summary": str}.
"""

_FILE_LIKE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,8}")


class JsonLlm(Protocol):
    """The same narrow capability the skeptic pass needs — one JSON call."""

    def complete_json(self, system: str, user: str) -> dict: ...


def summary_prompts(findings: list[GateVerdict]) -> tuple[str, str]:
    """The exact (system, user) pair `write_summary` sends, exposed so the
    caller can price the call before making it.
    """
    lines = []
    for verdict in findings:
        f = verdict.finding
        lines.append(
            f"- {f.severity.value.upper()} {f.file}:{f.line} [{f.category}] {f.title}\n"
            f"  code: {f.quoted_code.strip()}"
        )
    return _SUMMARY_SYSTEM_PROMPT, "Verified findings:\n" + "\n".join(lines)


def mentions_only_verified_files(summary: str, findings: list[GateVerdict]) -> bool:
    """True if every file-looking token in `summary` is a file some verified
    finding is about.

    Deliberately a whitelist check on the findings, not a blacklist of bad
    words: the failure being prevented is the summary inventing a location,
    and inventions cannot be enumerated in advance. A summary naming no file
    at all passes — it is a valid summary, just a general one.
    """
    allowed = {verdict.finding.file for verdict in findings}
    allowed_tails = {path.rsplit("/", 1)[-1] for path in allowed}
    for token in _FILE_LIKE.findall(summary):
        if token in allowed or token.rsplit("/", 1)[-1] in allowed_tails:
            continue
        # a bare version or decimal ("0.81", "3.11") is not a file reference
        if token.replace(".", "").isdigit():
            continue
        return False
    return True


def write_summary(findings: list[GateVerdict], llm: JsonLlm) -> str | None:
    """Return the summary text, or None when the caller should fall back to
    its own deterministic list.

    None is returned — rather than an exception raised or a partial string
    kept — for every failure: no findings to summarize, a broken call, an
    empty response, or a summary that failed the grounding check. The caller
    already knows how to render findings without help; the worst outcome
    here should be that this stage adds nothing.
    """
    if not findings:
        return None

    system, user = summary_prompts(findings)
    try:
        raw = llm.complete_json(system, user)
    except Exception:
        logger.info("summary_call_failed falling_back=deterministic_list")
        return None

    summary = str(raw.get("summary", "")).strip() if isinstance(raw, dict) else ""
    if not summary:
        return None

    if not mentions_only_verified_files(summary, findings):
        logger.info("summary_rejected reason=names_a_file_outside_the_verified_findings")
        return None

    return summary
