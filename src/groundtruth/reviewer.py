"""The one LLM call that proposes candidate findings.

Deliberately the least-trusted module in the pipeline, and it should stay
that way: everything it returns is a *claim*, not a fact, until it clears
every stage of `quality_gate` — the hallucination check, the adversarial
skeptic pass, the fingerprint dedupe. If this module hallucinated constantly,
the gate would still catch every fabrication; that's the whole reason
"propose" and "verify" are two separate modules instead of one LLM call
trusted to do both.
"""

from __future__ import annotations

import logging

from .context_engine import ContextBlock, DiffMap, FileDiff, Hunk, render_file_diff
from .llm import LlmClient
from .quality_gate.models import Finding, Severity

logger = logging.getLogger(__name__)

_REVIEW_SYSTEM_PROMPT = """\
You are a senior code reviewer. Find real problems in the CHANGED code shown
in <diff>. The <context> blocks (callers, related functions) are reference
material for checking contracts and conventions — they are NOT code under
review; never report a finding whose file/line isn't inside <diff>.

Check especially: contract mismatches (a function's signature changed but a
caller in <context> wasn't updated), logic errors, missing error handling,
resource handling, concurrency, security (injection, authz, hardcoded
secrets), and violations of conventions visible in <context>.

Rules:
- Every finding's "quoted_code" must be the EXACT text of a line that exists
  in <diff> (an added line) — not paraphrased, not reconstructed from memory.
- "file" and "line" must identify where in <diff> that quoted code appears.
- severity: "high" = wrong behavior/security/data loss; "medium" = likely bug
  or reliability gap; "low" = maintainability; "info" = worth knowing.
- confidence: 0.0-1.0, your own honest estimate — a second reviewer will
  independently check this finding before anyone sees it, so an inflated
  confidence doesn't make a weak finding stronger.
- If the code is fine, return an empty findings list. Do not invent problems
  to have something to say.

Review dimensions in scope for this pass: {dimensions}.

Reply with JSON: {{"findings": [{{"file": str, "line": int, "category": str,
"severity": "high"|"medium"|"low"|"info", "confidence": number,
"title": str, "quoted_code": str}}]}}.
"""


def system_prompt(dimensions: list[str] | None = None) -> str:
    dims = dimensions or ["correctness", "security", "conventions"]
    return _REVIEW_SYSTEM_PROMPT.format(dimensions=", ".join(dims))


def _render_context(context_blocks: list[ContextBlock]) -> str:
    if not context_blocks:
        return "(no additional context — the diff is self-contained)"
    return "\n\n".join(f"### {block.label}\n{block.text}" for block in context_blocks)


def _parse_finding(item: dict) -> Finding | None:
    try:
        return Finding(
            file=str(item["file"]),
            line=int(item["line"]),
            category=str(item["category"]),
            severity=Severity(str(item["severity"]).lower()),
            confidence=max(0.0, min(1.0, float(item["confidence"]))),
            title=str(item["title"]),
            quoted_code=str(item["quoted_code"]),
        )
    except (KeyError, ValueError, TypeError):
        # A malformed candidate is dropped, not fatal — one bad JSON object
        # in the response shouldn't cost every other finding in the batch.
        return None


def _log_prompt_size(
    llm: LlmClient, label: str, system: str, diff_text: str, context_text: str, user: str
) -> None:
    """Measure the ACTUAL prompt size sent per review call — not assumed
    from the configured token budget. Purely observational: this never
    changes what gets sent, and a measurement failure (a test double with no
    `estimate_cost`, say) must never break the real review underneath it.

    Why bother: `context_token_budget` says what `context_engine` is
    *allowed* to assemble, not what a call actually sends — the diff itself
    isn't budgeted at all today, and a limit on one half of a prompt says
    nothing about the combined total. Measuring first, before deciding
    whether either half needs a cap, is deliberate — the same "don't guess
    at prompt size, log it" discipline that surfaced a real bundling gap in
    the production system this project is modeled on.
    """
    try:
        diff_tokens = llm.estimate_cost(system="", user=diff_text).prompt_tokens
        context_tokens = llm.estimate_cost(system="", user=context_text).prompt_tokens
        combined_tokens = llm.estimate_cost(system=system, user=user).prompt_tokens
    except Exception:
        return
    logger.info(
        "review_call_prompt_size file=%s diff_tokens=%d context_tokens=%d combined_tokens=%d",
        label or "(unlabeled)", diff_tokens, context_tokens, combined_tokens,
    )


def propose_findings(
    diff_text: str,
    context_blocks: list[ContextBlock],
    llm: LlmClient,
    dimensions: list[str] | None = None,
    label: str = "",
) -> list[Finding]:
    """Ask the model to review `diff_text` and return whatever it proposes,
    parsed into `Finding`s. Returns `[]` on any failure (a broken call, a
    response that isn't valid JSON) — a failed review pass means "nothing to
    say this run," never a crash.

    `label` is only used for the prompt-size log line below — normally the
    file path being reviewed, so a size outlier in a big PR's logs can be
    traced back to which file caused it.
    """
    system = system_prompt(dimensions)
    context_text = _render_context(context_blocks)
    user = f"<diff>\n{diff_text}\n</diff>\n\n<context>\n{context_text}\n</context>"

    _log_prompt_size(llm, label, system, diff_text, context_text, user)

    try:
        raw = llm.complete_json(system, user)
    except Exception:
        return []

    items = raw.get("findings", []) if isinstance(raw, dict) else []
    findings = [_parse_finding(item) for item in items]
    return [f for f in findings if f is not None]


_CHARS_PER_TOKEN = 3.5  # same rough heuristic context_engine budgets with


def _hunk_tokens(hunk: Hunk) -> int:
    text = hunk.header + "\n" + "\n".join(hunk.lines)
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def hunk_groups(file_diff: FileDiff, max_tokens: int) -> list[list[Hunk]]:
    """Split one file's hunks into groups that each fit `max_tokens`.

    `context_token_budget` caps the context half of the prompt; nothing
    capped the diff half, so a single enormous file could still build a
    prompt far past any budget and quietly lose recall the same way a
    whole-PR call does. Splitting keeps every hunk in some call: the
    alternative, truncating the diff, would silently drop code from review
    and could hide the very change the bug is in.

    A hunk larger than the budget on its own still goes out alone rather
    than being cut in half — reviewing it whole is better than reviewing a
    fragment, and the size gets logged either way.
    """
    groups: list[list[Hunk]] = []
    current: list[Hunk] = []
    current_tokens = 0

    for hunk in file_diff.hunks:
        tokens = _hunk_tokens(hunk)
        if current and current_tokens + tokens > max_tokens:
            groups.append(current)
            current, current_tokens = [], 0
        current.append(hunk)
        current_tokens += tokens

    if current:
        groups.append(current)
    return groups


def propose_findings_for_diffmap(
    diffmap: DiffMap,
    context_blocks: list[ContextBlock],
    llm: LlmClient,
    dimensions: list[str] | None = None,
    max_diff_tokens_per_call: int | None = None,
) -> list[Finding]:
    """The real entry point `cli.py` uses — one review call per changed
    file, not one call over the whole PR.

    This exists because of a real, observed pattern: asking a model to
    exhaustively enumerate every problem across a large, multi-file diff in
    one shot loses recall as the diff grows — not because it runs out of
    context room, but because attention thins out across a bigger "find
    everything" task, so it reports the most-salient few issues and
    effectively stops looking. The symptom looks like a widening context
    window across repeated review-and-fix rounds; it's really just recall
    degrading with size on every single pass, including the first one.

    Reviewing one file at a time keeps each individual call's *target*
    bounded regardless of overall PR size — file 15 gets the same quality of
    attention as file 1. `context_blocks` (the callers, related functions
    `context_engine` assembled) stays the same across every file's call on
    purpose: the recall problem is specific to the size of what's being
    reviewed, not the size of the reference material around it.
    """
    findings: list[Finding] = []
    for path, file_diff in diffmap.items():
        if not file_diff.hunks:
            continue
        groups = (
            [file_diff.hunks]
            if max_diff_tokens_per_call is None
            else hunk_groups(file_diff, max_diff_tokens_per_call)
        )
        for index, group in enumerate(groups):
            label = path if len(groups) == 1 else f"{path} (part {index + 1}/{len(groups)})"
            group_text = render_file_diff(path, file_diff, group)
            findings.extend(
                propose_findings(group_text, context_blocks, llm, dimensions, label=label)
            )
    return findings
