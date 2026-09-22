"""Stage 2: a second, adversarial model cross-examines every finding.

The one idea worth remembering from this whole project: the skeptic's
confidence is **multiplied** against the original reviewer's confidence,
never averaged. Averaging lets a confident wrong answer outvote a skeptical
right one — (0.9 + 0.6) / 2 = 0.75 would still clear a 0.7 bar even though
the fact-checker had real doubt. Multiplying can't be talked over: 0.9 x 0.6
= 0.54 fails the same bar, because "what's the chance BOTH are right?" is
the actual question, and one confident and one unsure model both being right
is genuinely less likely than either alone. For a tool whose worst failure
mode is posting something false, making doubt contagious is the correct
asymmetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Finding

# The reason string a failed skeptic call drops a finding with. Named here
# so the caller can count these without matching on prose that might be
# reworded later.
SKEPTIC_CALL_FAILED = "skeptic call failed"

_SKEPTIC_SYSTEM_PROMPT = """\
You are a skeptical staff engineer auditing an AI code-review finding before
it is posted to a real pull request. Using ONLY the evidence provided —
never general knowledge about what's "usually" true — decide:

- is_real: is the claim factually correct about THIS code?
- is_actionable: would a reasonable senior developer actually want this
  fixed in this PR — not a style nitpick, not a hypothetical, not something
  already handled elsewhere?
- confidence: 0.0-1.0. When genuinely uncertain, prefer a LOWER number — a
  false positive costs more trust than a missed finding.

Reply with a JSON object: {"is_real": bool, "is_actionable": bool,
"confidence": number, "reason": string}.
"""


@dataclass(frozen=True)
class SkepticVerdict:
    is_real: bool
    is_actionable: bool
    confidence: float
    reason: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "SkepticVerdict":
        return cls(
            is_real=bool(data.get("is_real", False)),
            is_actionable=bool(data.get("is_actionable", False)),
            confidence=max(0.0, min(1.0, float(data.get("confidence", 0.0)))),
            reason=str(data.get("reason", "")),
        )


class JsonLlm(Protocol):
    """The only capability the skeptic pass needs from an LLM client —
    deliberately narrow so tests can pass a trivial fake instead of a real
    `LlmClient`, and so this module never has to know about providers,
    keys, or LiteLLM at all.
    """

    def complete_json(self, system: str, user: str) -> dict: ...


def skeptic_prompts(finding: Finding, evidence: str) -> tuple[str, str]:
    """The exact (system, user) pair `cross_examine` will send. Exposed so a
    caller can price the call before making it — an estimate built from a
    different prompt than the one that gets sent is not an estimate.
    """
    user_prompt = (
        f"Finding: {finding.title}\n"
        f"Category: {finding.category}\n"
        f"Quoted code: {finding.quoted_code}\n\n"
        f"Evidence (the surrounding code):\n{evidence}\n"
    )
    return _SKEPTIC_SYSTEM_PROMPT, user_prompt


def cross_examine(finding: Finding, evidence: str, llm: JsonLlm) -> SkepticVerdict:
    system_prompt, user_prompt = skeptic_prompts(finding, evidence)
    try:
        raw = llm.complete_json(system_prompt, user_prompt)
    except Exception:
        # A broken skeptic call must never crash the review. It degrades to
        # "could not verify" — treated as a real verdict of no confidence,
        # not silently skipped (skipping would let an unverified finding
        # through the same door as a verified one).
        return SkepticVerdict(
            is_real=False, is_actionable=False, confidence=0.0, reason=SKEPTIC_CALL_FAILED
        )
    return SkepticVerdict.from_json(raw)


def combined_confidence(reviewer_confidence: float, verdict: SkepticVerdict) -> float:
    """The multiply-not-average rule. Either gate (`is_real`,
    `is_actionable`) failing zeroes the finding outright — no amount of
    confidence rescues a claim the skeptic says is false or a nitpick.
    """
    if not (verdict.is_real and verdict.is_actionable):
        return 0.0
    return max(0.0, min(1.0, reviewer_confidence)) * verdict.confidence
