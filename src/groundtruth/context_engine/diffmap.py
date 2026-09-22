"""Parse a unified diff into a per-file map of hunks and added lines.

This is the one thing every other piece of the review depends on: which files
changed, which lines were added (1-indexed, matching the new/head version of
the file), and where each hunk sits. Nothing here is language-aware — it's
plain text parsing of the unified diff format git/GitHub/GitLab/Bitbucket all
produce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+)$")
_OLD_FILE_HEADER_RE = re.compile(r"^--- (?:a/)?(?P<path>.+)$")
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class Hunk:
    """One contiguous block of change within a file."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    added_lines: tuple[int, ...]  # 1-indexed line numbers in the new file
    removed_count: int
    header: str = ""  # the raw "@@ -a,b +c,d @@ ..." line
    lines: tuple[str, ...] = ()  # raw body lines, each still carrying its +/-/space prefix

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count - 1, 0)


@dataclass
class FileDiff:
    path: str
    is_new: bool = False
    is_deleted: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    added: set[int] = field(default_factory=set)  # union of every hunk's added_lines


# file path -> its diff
DiffMap = dict[str, FileDiff]


def parse_diff(diff_text: str) -> DiffMap:
    """Parse a unified diff (as produced by `git diff`, a GitHub/GitLab/
    Bitbucket API diff endpoint, or `git format-patch`) into a DiffMap.

    Deliberately forgiving: unparseable or binary-looking sections are
    skipped rather than raising, because a partially-broken diff should
    degrade the review, not crash it.
    """
    diffmap: DiffMap = {}
    current: FileDiff | None = None
    added_in_hunk: list[int] = []
    removed_in_hunk = 0
    body_lines: list[str] = []
    pending_hunk_header: dict | None = None

    def flush_hunk() -> None:
        nonlocal added_in_hunk, removed_in_hunk, body_lines, pending_hunk_header
        if current is not None and pending_hunk_header is not None:
            current.hunks.append(
                Hunk(
                    old_start=pending_hunk_header["old_start"],
                    old_count=pending_hunk_header["old_count"],
                    new_start=pending_hunk_header["new_start"],
                    new_count=pending_hunk_header["new_count"],
                    added_lines=tuple(added_in_hunk),
                    removed_count=removed_in_hunk,
                    header=pending_hunk_header["raw"],
                    lines=tuple(body_lines),
                )
            )
            current.added.update(added_in_hunk)
        added_in_hunk = []
        removed_in_hunk = 0
        body_lines = []
        pending_hunk_header = None

    old_line = 0
    new_line = 0

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            flush_hunk()
            current = None
            continue

        if raw_line.startswith("--- "):
            # Old-side header; only tells us about deletion (--- a/x vs /dev/null
            # means the file used to exist). Path of record comes from +++.
            continue

        m = _FILE_HEADER_RE.match(raw_line)
        if m:
            flush_hunk()
            path = m.group("path")
            if path == "/dev/null":
                # A pure deletion has no "new" side worth tracking context for.
                current = None
                continue
            current = diffmap.setdefault(path, FileDiff(path=path))
            continue

        if current is None:
            continue

        hm = _HUNK_HEADER_RE.match(raw_line)
        if hm:
            flush_hunk()
            old_start = int(hm.group("old_start"))
            new_start = int(hm.group("new_start"))
            old_count = int(hm.group("old_count") or "1")
            new_count = int(hm.group("new_count") or "1")
            pending_hunk_header = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "raw": raw_line,
            }
            old_line = old_start
            new_line = new_start
            continue

        if pending_hunk_header is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            added_in_hunk.append(new_line)
            new_line += 1
            body_lines.append(raw_line)
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            removed_in_hunk += 1
            old_line += 1
            body_lines.append(raw_line)
        elif raw_line.startswith("\\"):
            # "\ No newline at end of file" — not a content line.
            continue
        else:
            # Context line (starts with a space, or empty).
            old_line += 1
            new_line += 1
            body_lines.append(raw_line)

    flush_hunk()
    return diffmap


def render_file_diff(path: str, file_diff: FileDiff, hunks: list[Hunk] | None = None) -> str:
    """Reconstruct a standalone, readable diff snippet for just this one
    file — what a per-file review batch actually gets shown (see
    `reviewer.propose_findings_for_diffmap`), and still substring-matchable
    by `quality_gate`'s hallucination check, since it's built from the exact
    same raw lines the original diff had.

    `hunks` renders a subset of the file's hunks instead of all of them, for
    a file whose whole diff would not fit one review call's budget.
    """
    lines = [f"--- a/{path}", f"+++ b/{path}"]
    for hunk in file_diff.hunks if hunks is None else hunks:
        lines.append(hunk.header)
        lines.extend(hunk.lines)
    return "\n".join(lines)
