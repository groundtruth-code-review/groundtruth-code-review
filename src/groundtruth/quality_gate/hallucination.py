"""Stage 1: does the evidence a finding cites actually exist?

The rule: every finding must quote the exact code it's about, and that quote
must appear — verbatim, modulo whitespace — somewhere in the diff or the
assembled context. A model can *say* anything; it can't photocopy a line
that was never written. This is the single check that turns "the model
claims X" into "X is provably true of this code," before any judgment about
whether the claim is even a good one gets made.

Whitespace is stripped entirely before comparing, because models routinely
re-indent or rejoin lines when quoting — an honest quote shouldn't fail over
an invisible space. Quotes shorter than the minimum length are rejected
outright: something like `x = 1` would "match" almost any codebase and
proves nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

_MIN_QUOTE_LENGTH = 8


def _normalize(text: str) -> str:
    return "".join(text.split())


def line_in_changed_hunk(line: int, hunk_spans: Iterable[tuple[int, int]]) -> bool:
    """True if `line` (1-indexed, new/head side) falls inside one of the
    file's changed hunks.

    A quote can be real while the line number attached to it is wrong — the
    model copies a line correctly and then misreports where it sits. That
    finding survives the quote check, and the failure only shows up at the
    very end, when the platform refuses to place a comment on a line that
    isn't part of the diff. Checking here turns a silent posting failure
    into an ordinary, recorded rejection.

    The whole hunk span counts, not just its added lines: a model often
    points at the function signature a few lines above its own change, that
    line is part of the diff the platform will accept a comment on, and
    being stricter would throw away good findings to fix a bookkeeping bug.
    """
    return any(start <= line <= end for start, end in hunk_spans)


def quote_exists(quoted_code: str, evidence_texts: list[str]) -> bool:
    """True if `quoted_code` is a genuine (whitespace-insensitive) substring
    of at least one of `evidence_texts` (the diff's added lines, plus every
    context block the reviewer was shown).
    """
    if len(quoted_code.strip()) < _MIN_QUOTE_LENGTH:
        return False
    needle = _normalize(quoted_code)
    return any(needle in _normalize(evidence) for evidence in evidence_texts)
