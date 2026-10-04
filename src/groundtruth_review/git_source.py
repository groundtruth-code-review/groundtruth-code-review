"""Thin wrapper around `git` — the only place in this project that shells out
to it. `context_engine` never touches git directly; it just wants a diff
string and `{path: file_text}` dicts, which is exactly what this module
produces from a repo and two refs. That separation is what let the engine's
own test suite run against synthetic repos with zero git commits at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def git_diff(repo: Path | str, base: str, head: str) -> str:
    """The unified diff between `base` and `head`, against their merge base
    (`base...head`) — the same comparison a pull request's "Files changed"
    tab shows, not a raw two-dot diff of both branches' tips.
    """
    repo = Path(repo)
    result = _run(repo, "diff", f"{base}...{head}")
    if result.returncode != 0:
        raise GitError(f"git diff {base}...{head} failed: {result.stderr.strip()}")
    return result.stdout


def show_file(repo: Path | str, ref: str, path: str) -> str | None:
    """The file's content at `ref`, or `None` if it doesn't exist there (a
    newly-added file has no base-side content; a deleted one has no
    head-side content) — never an exception for that, which is the normal
    case, not an error.
    """
    result = _run(Path(repo), "show", f"{ref}:{path}")
    if result.returncode != 0:
        return None
    return result.stdout


def load_sources(repo: Path | str, ref: str, paths: list[str]) -> dict[str, str]:
    """`{path: content}` for every path that actually exists at `ref`. Paths
    missing at this ref (new files at the base ref, deleted files at head)
    are simply absent from the result — callers already treat a missing
    entry as "nothing to compare," not as a failure.
    """
    sources: dict[str, str] = {}
    for path in paths:
        content = show_file(repo, ref, path)
        if content is not None:
            sources[path] = content
    return sources


def merge_base(repo: Path | str, base: str, head: str) -> str:
    """The actual common ancestor commit — used to detect a rebase/force-push
    that broke the "last reviewed sha is an ancestor of the new head"
    assumption an incremental re-review would otherwise rely on.
    """
    result = _run(Path(repo), "merge-base", base, head)
    if result.returncode != 0:
        raise GitError(f"git merge-base {base} {head} failed: {result.stderr.strip()}")
    return result.stdout.strip()
