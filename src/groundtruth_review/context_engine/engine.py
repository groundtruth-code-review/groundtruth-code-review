"""Assemble the actual context payload sent to the reviewer.

This is the recipe: for every changed line, find the whole function it lives
in (never review a line in isolation); for every changed function, find who
calls it and whether its signature changed since the base commit (a changed
signature promotes its callers from "nice to have" to "must include" — a
caller still passing the old argument list is exactly the bug a diff-only
review cannot see); for the code a change adds, find the definitions of the
functions it starts calling; follow callers one or two more steps out; then
pack everything into a fixed token budget, dropping the least important
pieces first and always saying what was dropped.

Every step here is a rule, not a decision the model makes: the same diff
builds the same context, and the cost of sending it is known beforehand.

No LLM call happens here. This module's whole job is deciding what an LLM
*should* see, not talking to one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .callers import find_callers, find_definitions
from .chunker import CodeChunk, enclosing_chunks, find_by_name, parse_file
from .diffmap import DiffMap

_CHARS_PER_TOKEN = 3.5  # same rough heuristic the original design used


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


@dataclass
class ContextBlock:
    label: str
    text: str
    # 0 = the changed function itself, 1 = a direct caller, 2 = a function the
    # changed code calls, 3+ = a caller further out (one more tier per hop).
    # Higher tiers are degraded and dropped first when the budget runs short.
    tier: int
    must_include: bool = False

    @property
    def tokens(self) -> int:
        return _estimate_tokens(self.text)


@dataclass(frozen=True)
class SignatureChange:
    """A function whose header differs between the base and the head, with
    where it sits in the head file. Recorded here, where the comparison is
    already made, so nothing downstream has to redo it.
    """

    path: str
    name: str
    start_line: int
    end_line: int


@dataclass
class ReviewContext:
    blocks: list[ContextBlock] = field(default_factory=list)
    estimated_tokens: int = 0
    cut: list[str] = field(default_factory=list)  # labels dropped/degraded, for transparency
    signature_changes: list[SignatureChange] = field(default_factory=list)


def _normalized_header(header: str) -> str:
    return " ".join(header.split())


def _signature(chunk) -> str:
    """What a function's signature is, for deciding whether it changed.

    The header is only the first line, and for a signature wrapped one
    parameter per line (what Black produces for anything long) the first
    line is `def f(` before and after a parameter is added -- so comparing
    headers called that unchanged. This reads through the line where the
    parameter list closes. A signature that fits on one line comes out
    exactly as the header did, return annotation included.
    """
    text = chunk.text
    open_at = text.find("(")
    if open_at != -1:
        depth = 0
        for i in range(open_at, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = text.find("\n", i)
                    return _normalized_header(text if end == -1 else text[:end])
    return _normalized_header(chunk.header)  # no parameter list we can read: first line, as before


# Calls worth looking up: a bare `name(` that is not a method on something,
# plus `self.name(` / `cls.name(` / `this.name(`, which are calls into the same
# class. `obj.name(` is left out on purpose -- `.get(` on a dict and `.get(` on
# this repo's own class look identical to a text search.
_BARE_CALL = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
_SELF_CALL = re.compile(r"\b(?:self|cls|this)\.([A-Za-z_]\w*)\s*\(")
_DEFINES = ("def", "function", "func", "fn")
_NOT_CALLEES = frozenset(
    """if elif else for while switch catch with return yield await assert raise try except finally
    and or not in is lambda class def function func fn new delete typeof sizeof
    print len range str int float bool list dict set tuple bytes type object super isinstance issubclass
    open sorted reversed min max sum any all map filter zip enumerate repr id iter next hash round abs
    getattr setattr hasattr callable format join append extend""".split()
)

_MAX_LOOKUPS_PER_CHUNK = 10  # names looked up per changed function, resolved or not
_MAX_DEFINITION_SEARCHES = 60  # repository searches for definitions, per review
_MAX_INDIRECT_SEARCHES = 25  # caller searches beyond the first hop, per review


def _callee_names(lines: list[str], own_name: str) -> list[str]:
    """Names of functions called on these lines, in order of first appearance."""
    names: list[str] = []
    for line in lines:
        if line.lstrip().startswith(("#", "//", "*", "/*")):
            continue
        for rx in (_BARE_CALL, _SELF_CALL):
            for match in rx.finditer(line):
                name = match.group(1)
                before = line[: match.start()].rstrip()
                if before.endswith(_DEFINES):  # `def name(` defines it; it doesn't call it
                    continue
                if name == own_name or name in _NOT_CALLEES or len(name) < 3 or name in names:
                    continue
                names.append(name)
    return names


class _Files:
    """Reads and parses each file at most once per review."""

    def __init__(self, root: Path):
        self.root = root
        self._chunks: dict[str, list[CodeChunk] | None] = {}

    def chunks(self, path: str) -> list[CodeChunk] | None:
        if path not in self._chunks:
            try:
                source = (self.root / path).read_text(encoding="utf-8", errors="ignore")
                self._chunks[path] = parse_file(source, path)
            except OSError:
                self._chunks[path] = None
        return self._chunks[path]

    def chunk_at(self, path: str, line: int, innermost: bool = False) -> CodeChunk | None:
        found = [c for c in (self.chunks(path) or []) if c.start_line <= line <= c.end_line]
        if not found:
            return None
        return min(found, key=lambda c: c.size) if innermost else found[0]


def build_context(
    repo_root: Path | str,
    diffmap: DiffMap,
    head_sources: dict[str, str],
    base_sources: dict[str, str] | None = None,
    budget_tokens: int = 25_000,
    max_callers_per_symbol: int = 5,
    caller_depth: int = 2,
    include_callees: bool = True,
    max_indirect_callers: int = 2,
    max_callees_per_chunk: int = 4,
) -> ReviewContext:
    """Build the labeled, budgeted context payload for one review.

    `head_sources` / `base_sources` map repo-relative path -> full file text,
    for the new and old side of the diff respectively. `base_sources` is
    optional — without it, signature-change detection is simply skipped, not
    an error (the review still works, it just can't promote callers on a
    changed contract).

    `caller_depth` is how many steps out callers are followed: 1 is the
    functions that call a changed one, 2 adds who calls those. Callers past
    the first step are optional context and are the first thing the budget
    drops. `include_callees` adds the definitions of functions the changed
    lines start calling, so the reviewer can see what a new call actually
    does. Both are bounded (`max_indirect_callers`, `max_callees_per_chunk`,
    and a fixed cap on searches per review) so a large pull request cannot
    turn into a search of the whole repository.
    """
    root = Path(repo_root)
    base_sources = base_sources or {}
    files = _Files(root)

    candidates: list[ContextBlock] = []
    seen_labels: set[str] = set()
    signature_changed_names: set[str] = set()
    signature_changes: list[SignatureChange] = []

    # (path, start_line, end_line) of every chunk included as changed code. The
    # other kinds of block are held back and filtered against it, rather than
    # appended inline, because the chunk one of them duplicates may live in a
    # file this loop has not reached yet.
    Span = tuple[str, int, int]
    changed_spans: set[Span] = set()
    caller_candidates: list[tuple[Span, ContextBlock]] = []
    callee_candidates: list[tuple[Span, ContextBlock]] = []
    # direct callers, as (path, chunk, the changed function they lead back to)
    frontier: list[tuple[str, CodeChunk, str]] = []
    # definition lookups are repository searches: remember each answer, and stop
    # searching once a review has spent its allowance
    definitions_seen: dict[str, list] = {}

    for path, filediff in diffmap.items():
        if not filediff.added or path not in head_sources:
            continue

        head_chunks = parse_file(head_sources[path], path)
        changed_chunks = enclosing_chunks(head_chunks, filediff.added)
        head_lines = head_sources[path].splitlines()

        base_chunks = parse_file(base_sources[path], path) if path in base_sources else []

        for chunk in changed_chunks:
            base_match = find_by_name(base_chunks, chunk.name) if base_chunks else None
            changed_signature = bool(base_match and _signature(base_match) != _signature(chunk))
            if changed_signature:
                signature_changed_names.add(chunk.name)
                signature_changes.append(
                    SignatureChange(path, chunk.name, chunk.start_line, chunk.end_line)
                )

            sig_note = ": signature changed" if changed_signature else ""
            label = f"{path}#L{chunk.start_line}-{chunk.end_line} (changed{sig_note})"
            changed_spans.add((path, chunk.start_line, chunk.end_line))
            if label not in seen_labels:
                seen_labels.add(label)
                candidates.append(
                    ContextBlock(label=label, text=chunk.text, tier=0, must_include=True)
                )

            for hit in find_callers(root, chunk.name, from_path=path, limit=max_callers_per_symbol):
                if hit.path == path and chunk.start_line <= hit.line <= chunk.end_line:
                    continue  # a call from inside the very function we already included
                caller_chunk = files.chunk_at(hit.path, hit.line)
                if caller_chunk is None:
                    continue

                promoted = chunk.name in signature_changed_names
                clabel = (
                    f"{hit.path}#L{caller_chunk.start_line}-{caller_chunk.end_line} "
                    f"(caller of {chunk.name}{', signature changed' if promoted else ''})"
                )
                if clabel in seen_labels:
                    continue
                seen_labels.add(clabel)
                caller_candidates.append(
                    (
                        (hit.path, caller_chunk.start_line, caller_chunk.end_line),
                        ContextBlock(
                            label=clabel,
                            text=caller_chunk.text,
                            tier=1,
                            must_include=promoted,
                        ),
                    )
                )
                frontier.append((hit.path, caller_chunk, chunk.name))

            if include_callees:
                # Only what the change adds: a call that was already there was
                # reviewed when it was written, and this is a bounded budget.
                added_here = [
                    head_lines[n - 1]
                    for n in sorted(filediff.added)
                    if chunk.start_line <= n <= chunk.end_line and n <= len(head_lines)
                ]
                resolved = 0
                for name in _callee_names(added_here, chunk.name)[:_MAX_LOOKUPS_PER_CHUNK]:
                    if resolved >= max_callees_per_chunk:
                        break
                    if name not in definitions_seen:
                        if len(definitions_seen) >= _MAX_DEFINITION_SEARCHES:
                            break
                        try:
                            definitions_seen[name] = find_definitions(root, name)
                        except Exception:
                            definitions_seen[name] = []  # a failed lookup costs a block, never the review
                    definitions = definitions_seen[name]
                    if len(definitions) != 1:
                        continue  # no definition, or several: guessing which is worse than silence
                    definition = definitions[0]
                    callee = files.chunk_at(definition.path, definition.line, innermost=True)
                    if callee is None or callee.name != name:
                        continue
                    callee_label = (
                        f"{definition.path}#L{callee.start_line}-{callee.end_line} "
                        f"(callee of {chunk.name})"
                    )
                    if callee_label in seen_labels:
                        continue
                    seen_labels.add(callee_label)
                    resolved += 1
                    callee_candidates.append(
                        (
                            (definition.path, callee.start_line, callee.end_line),
                            ContextBlock(label=callee_label, text=callee.text, tier=2),
                        )
                    )

    # Callers further out. Each step searches for the callers of the previous
    # step's callers, so the number of searches is capped for the whole review.
    indirect_candidates: list[tuple[Span, ContextBlock]] = []
    searches = 0
    for hop in range(2, caller_depth + 1):
        next_frontier: list[tuple[str, CodeChunk, str]] = []
        for via_path, via, origin in frontier:
            if searches >= _MAX_INDIRECT_SEARCHES:
                break
            searches += 1
            for hit in find_callers(root, via.name, from_path=via_path, limit=max_indirect_callers):
                if hit.path == via_path and via.start_line <= hit.line <= via.end_line:
                    continue  # the function calling itself
                outer = files.chunk_at(hit.path, hit.line)
                if outer is None:
                    continue
                label = (
                    f"{hit.path}#L{outer.start_line}-{outer.end_line} "
                    f"(caller of {via.name}, {hop} hops from {origin})"
                )
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                indirect_candidates.append(
                    (
                        (hit.path, outer.start_line, outer.end_line),
                        ContextBlock(label=label, text=outer.text, tier=hop + 1),
                    )
                )
                next_frontier.append((hit.path, outer, origin))
        frontier = next_frontier

    # One function must not be sent twice under two labels. A changed function
    # that also calls the changed symbol would otherwise appear as changed code
    # and again as its own caller; a callee can be a function that is changed in
    # this very pull request; a caller two steps out can already be a direct
    # caller. The earlier, higher-priority block already carries the text, so
    # the copy adds nothing but cost.
    used_spans = set(changed_spans)
    for group in (caller_candidates, callee_candidates, indirect_candidates):
        for span, block in group:
            if span in used_spans:
                continue
            used_spans.add(span)
            candidates.append(block)

    # Pack by priority: must-include first, then tier, then declaration order
    # (stable sort preserves that). Degrade optional callers to header-only
    # before dropping them outright; must-include blocks are never dropped —
    # the honesty principle is "budget can shrink detail, never hide the fact
    # that this code exists."
    candidates.sort(key=lambda b: (not b.must_include, b.tier))

    ctx = ReviewContext(signature_changes=signature_changes)
    for block in candidates:
        if ctx.estimated_tokens + block.tokens <= budget_tokens or block.must_include:
            ctx.blocks.append(block)
            ctx.estimated_tokens += block.tokens
            continue

        degraded_text = block.text.splitlines()[0] if block.text else ""
        degraded = ContextBlock(
            label=block.label + " [truncated to fit the context budget]",
            text=degraded_text,
            tier=block.tier,
            must_include=block.must_include,
        )
        if ctx.estimated_tokens + degraded.tokens <= budget_tokens:
            ctx.blocks.append(degraded)
            ctx.estimated_tokens += degraded.tokens
            ctx.cut.append(block.label + " (degraded to signature only)")
        else:
            ctx.cut.append(block.label + " (dropped)")

    return ctx
