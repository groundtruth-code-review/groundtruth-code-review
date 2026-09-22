"""Find call sites of a symbol across a repository.

Prefers the real `rg` (ripgrep) binary when it's on PATH — it's dramatically
faster on large repos. Falls back to a pure-Python directory walk otherwise,
so the engine works on any machine, not just ones with ripgrep installed.
Both paths apply the same skip list, extension filter and size cap: a search
that returns different results depending on which binaries are installed is
a search nobody can reason about.
That fallback isn't a compromise bolted on for this rebuild: the original
design anticipated exactly this ("rg or pure-python") because ripgrep being
missing was always a real possibility on some worker, not a hypothetical.

Matching is name-based (`\\bNAME\\s*\\(`), which over-matches on common names —
two unrelated `process()` functions in different files both show up. That's a
known, accepted trade-off (see the project README): a dumb, explainable search
beats a clever one nobody can debug, and the quality gate's skeptic pass is
what filters the noise this produces, not the search itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}

# Extensions worth searching. Kept short and deliberately unglamorous —
# binary/asset files never contain a call site worth finding.
_SEARCHABLE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".kt", ".swift", ".scala",
}

_MAX_FILE_BYTES = 2_000_000  # skip anything absurdly large rather than choke on it


@dataclass(frozen=True)
class CallerHit:
    path: str  # repo-relative
    line: int  # 1-indexed
    text: str  # the matching line, stripped


def _name_pattern(name: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(name) + r"\s*\(")


# A line containing "def NAME(" / "function NAME(" / "func NAME(" / "fn NAME("
# is the definition, not a call — never count it as a caller. This is a
# heuristic, not a parse: languages whose method syntax has no such keyword
# (Java, C#, C++ — "ReturnType methodName(args)") can still slip a definition
# through as a false "caller." That's an accepted, documented gap, same shape
# as the name-matching over-match itself — the quality gate's skeptic pass is
# the real backstop, not this search.
def _is_definition_line(line: str, name: str) -> bool:
    return bool(
        re.search(r"\b(?:def|function|func|fn)\s+" + re.escape(name) + r"\s*\(", line)
    )


def _rank_key(hit: CallerHit, from_path: str) -> tuple[int, str, int]:
    """Rank by shared directory depth with the file being searched from —
    closer files are more likely to be genuinely related, not just a
    same-named coincidence."""
    from_parts = Path(from_path).parent.parts
    hit_parts = Path(hit.path).parent.parts
    shared = 0
    for a, b in zip(from_parts, hit_parts):
        if a != b:
            break
        shared += 1
    return (-shared, hit.path, hit.line)


def rg_filters() -> list[str]:
    """The skip list, the extension list and the size cap, expressed as
    ripgrep arguments.

    Built from the same constants the pure-Python walk uses, because the two
    paths answering differently is worse than either answer: with ripgrep
    installed the search used to reach into `node_modules`, `vendor` and
    `dist`, so a review's context could be spent on third-party code, and a
    finding could be raised against a file nobody in the repository owns.
    The behaviour of the search should not depend on which binaries happen
    to be on a machine.
    """
    args: list[str] = []
    for directory in sorted(_SKIP_DIRS):
        args += ["--glob", f"!**/{directory}/**"]
    for ext in sorted(_SEARCHABLE_EXT):
        args += ["--glob", f"*{ext}"]
    args += ["--max-filesize", str(_MAX_FILE_BYTES)]
    return args


def _search_with_rg(repo_root: Path, name: str) -> list[CallerHit]:
    pattern = r"\b" + re.escape(name) + r"\s*\("
    proc = subprocess.run(
        [
            "rg", "--line-number", "--no-heading", "--max-count", "40",
            *rg_filters(), pattern, str(repo_root),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    hits: list[CallerHit] = []
    for line in proc.stdout.splitlines():
        # rg --no-heading output: <path>:<line>:<text>
        try:
            path_str, line_no, text = line.split(":", 2)
        except ValueError:
            continue
        if _is_definition_line(text, name):
            continue
        rel = str(Path(path_str).resolve().relative_to(repo_root.resolve()))
        hits.append(CallerHit(path=rel, line=int(line_no), text=text.strip()))
    return hits


def _search_pure_python(repo_root: Path, name: str) -> list[CallerHit]:
    pattern = _name_pattern(name)
    hits: list[CallerHit] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in _SEARCHABLE_EXT:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and not _is_definition_line(line, name):
                hits.append(
                    CallerHit(path=str(path.relative_to(repo_root)), line=i, text=line.strip())
                )
    return hits


def find_callers(
    repo_root: Path | str,
    name: str,
    from_path: str = "",
    limit: int = 5,
) -> list[CallerHit]:
    """Search `repo_root` for call sites of `name`, ranked by directory
    proximity to `from_path` (the file the symbol is defined in), capped at
    `limit`. Returns `[]` on any search failure — a missed caller degrades
    the review's completeness, never crashes it.
    """
    root = Path(repo_root)
    if len(name) < 2:
        return []

    try:
        if shutil.which("rg"):
            hits = _search_with_rg(root, name)
        else:
            hits = _search_pure_python(root, name)
    except Exception:
        return []

    hits.sort(key=lambda h: _rank_key(h, from_path))
    return hits[:limit]
