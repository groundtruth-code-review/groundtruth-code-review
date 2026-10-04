"""Turn source files into function/class-level chunks using Tree-sitter.

This is the piece that lets the reviewer think in whole functions instead of
arbitrary line windows: "line 23 changed" becomes "the 12-line function that
line lives in changed." No LLM call anywhere in this module — it's parsing.

Grammars come from `tree-sitter-language-pack`, which ships 300+ pre-built
grammars and downloads the ones actually needed on first use (cached after
that). A language with no available grammar, or a file that fails to parse,
degrades to an empty chunk list rather than raising — callers fall back to
treating the whole file as one chunk. Never crash the review over one file.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import tree_sitter_language_pack as tslp
except ImportError:  # pragma: no cover - exercised only if the dep is missing
    tslp = None

# Structural kinds that behave like "a function": the units we want the
# reviewer to reason about. Class/struct/interface bodies still get walked
# (their methods show up as children), but the container itself isn't a
# useful standalone review unit the way a function or method is.
_LEAF_KINDS = {"Function", "Method", "Arrow", "Constructor"}


@dataclass(frozen=True)
class CodeChunk:
    """One function/method-sized unit of source, 1-indexed inclusive lines."""

    name: str
    kind: str
    start_line: int
    end_line: int
    header: str  # first line of the chunk — the thing signature-diffing compares
    text: str

    @property
    def size(self) -> int:
        return self.end_line - self.start_line + 1


def _ensure_language(language: str) -> bool:
    if tslp is None:
        return False
    try:
        if tslp.has_language(language):
            return True
        return tslp.download([language]) > 0 or tslp.has_language(language)
    except Exception:
        # Offline, unknown language, whatever — degrade, don't crash.
        return False


def _flatten(items, source_lines: list[str], out: list[CodeChunk]) -> None:
    for item in items:
        span = item.span
        start_line = span.start_line + 1  # tree-sitter-language-pack is 0-indexed
        end_line = span.end_line + 1
        if 1 <= start_line <= len(source_lines):
            header = source_lines[start_line - 1].strip()
        else:
            header = item.name or ""
        text = "\n".join(source_lines[start_line - 1 : end_line])
        if item.kind in _LEAF_KINDS or not item.children:
            out.append(
                CodeChunk(
                    name=item.name or "<anonymous>",
                    kind=item.kind,
                    start_line=start_line,
                    end_line=end_line,
                    header=header,
                    text=text,
                )
            )
        if item.children:
            _flatten(item.children, source_lines, out)


def parse_file(source: str, path: str, language: str | None = None) -> list[CodeChunk]:
    """Parse one file's source into function/method-level chunks.

    `language` can be forced (useful for tests); otherwise it's detected from
    the file extension. Returns `[]` if the language is unsupported, ungrammared,
    or the source fails to parse — that's the signal to the rest of the engine
    to fall back to whole-file context for this one file.
    """
    if tslp is None:
        return []
    lang = language or tslp.detect_language_from_path(path)
    if not lang or not _ensure_language(lang):
        return []
    try:
        result = tslp.process(source, tslp.ProcessConfig(language=lang))
    except Exception:
        return []

    source_lines = source.splitlines()
    chunks: list[CodeChunk] = []
    _flatten(result.structure, source_lines, chunks)
    return chunks


def enclosing_chunks(chunks: list[CodeChunk], lines: set[int]) -> list[CodeChunk]:
    """For each changed line, find its smallest (innermost) enclosing chunk.

    A line inside a method inside a class matches the method, not the class —
    the method is the actual unit of review. Lines that fall inside no known
    chunk (e.g. module-level code) simply contribute nothing here; the whole
    diff hunk is still reviewed, just without an extra "enclosing function"
    context block for that line.
    """
    chosen: dict[tuple[str, int, int], CodeChunk] = {}
    for line in lines:
        best: CodeChunk | None = None
        for chunk in chunks:
            if chunk.start_line <= line <= chunk.end_line:
                if best is None or chunk.size < best.size:
                    best = chunk
        if best is not None:
            chosen[(best.name, best.start_line, best.end_line)] = best
    return sorted(chosen.values(), key=lambda c: c.start_line)


def find_by_name(chunks: list[CodeChunk], name: str) -> CodeChunk | None:
    for chunk in chunks:
        if chunk.name == name:
            return chunk
    return None
