"""A finding's identity — the gate's memory, used for dedupe and auto-resolve.

The recipe is deliberately **facts-only**: `file | hunk-content-hash |
category | quoted-code-hash`. Nothing the model *wrote* (its title, its
phrasing) is part of the identity — only what it *points at*. That
distinction was learned the hard way in the system this project is modeled
on: an early recipe that included the model's own title broke the very
first time the model re-worded an unchanged finding between two runs on
identical code (LLMs reliably vary their phrasing; that's not a bug to fix
in the model, it's a fact to design around). The fingerprint changing when
nothing else did meant the same bug got posted twice — and worse, under an
auto-resolve feature, a *rewrite* could make a live bug read as "‚úÖ resolved."

Two properties this recipe guarantees, both covered by tests:
  1. Editing the flagged code always produces a new fingerprint — content
     that changed is always re-judged from zero, never silently carried
     forward.
  2. Code that *didn't* change keeps the same fingerprint no matter how the
     model rewords its explanation — memory only ever silences findings on
     untouched code.
"""

from __future__ import annotations

import hashlib


def _content_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def fingerprint(file: str, hunk_text: str, category: str, quoted_code: str) -> str:
    """A stable id for "this finding, about this exact code." Survives
    rebases and line-number drift (nothing here is a line number); does NOT
    survive an edit to the flagged code itself — that's intentional.
    """
    parts = "|".join(
        [file, _content_hash(hunk_text), category, _content_hash(quoted_code)]
    )
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()
