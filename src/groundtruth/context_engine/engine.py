"""Assemble the actual context payload sent to the reviewer.

This is the recipe: for every changed line, find the whole function it lives
in (never review a line in isolation); for every changed function, find who
calls it and whether its signature changed since the base commit (a changed
signature promotes its callers from "nice to have" to "must include" — a
caller still passing the old argument list is exactly the bug a diff-only
review cannot see); then pack everything into a fixed token budget, dropping
the least important pieces first and always saying what was dropped.

No LLM call happens here. This module's whole job is deciding what an LLM
*should* see, not talking to one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .callers import find_callers
from .chunker import enclosing_chunks, find_by_name, parse_file
from .diffmap import DiffMap

_CHARS_PER_TOKEN = 3.5  # same rough heuristic the original design used


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


@dataclass
class ContextBlock:
    label: str
    text: str
    tier: int  # 0 = the changed function itself, 1 = a caller, 2 = everything else
    must_include: bool = False

    @property
    def tokens(self) -> int:
        return _estimate_tokens(self.text)


@dataclass
class ReviewContext:
    blocks: list[ContextBlock] = field(default_factory=list)
    estimated_tokens: int = 0
    cut: list[str] = field(default_factory=list)  # labels dropped/degraded, for transparency


def _normalized_header(header: str) -> str:
    return " ".join(header.split())


def build_context(
    repo_root: Path | str,
    diffmap: DiffMap,
    head_sources: dict[str, str],
    base_sources: dict[str, str] | None = None,
    budget_tokens: int = 25_000,
    max_callers_per_symbol: int = 5,
) -> ReviewContext:
    """Build the labeled, budgeted context payload for one review.

    `head_sources` / `base_sources` map repo-relative path -> full file text,
    for the new and old side of the diff respectively. `base_sources` is
    optional — without it, signature-change detection is simply skipped, not
    an error (the review still works, it just can't promote callers on a
    changed contract).
    """
    root = Path(repo_root)
    base_sources = base_sources or {}

    candidates: list[ContextBlock] = []
    seen_labels: set[str] = set()
    signature_changed_names: set[str] = set()

    # (path, start_line, end_line) of every chunk included as changed code, and
    # the caller blocks waiting to be filtered against it. Callers are held
    # back rather than appended inline because the chunk a caller duplicates
    # may live in a file this loop has not reached yet.
    changed_spans: set[tuple[str, int, int]] = set()
    caller_candidates: list[tuple[tuple[str, int, int], ContextBlock]] = []

    for path, filediff in diffmap.items():
        if not filediff.added or path not in head_sources:
            continue

        head_chunks = parse_file(head_sources[path], path)
        changed_chunks = enclosing_chunks(head_chunks, filediff.added)

        base_chunks = parse_file(base_sources[path], path) if path in base_sources else []

        for chunk in changed_chunks:
            base_match = find_by_name(base_chunks, chunk.name) if base_chunks else None
            changed_signature = bool(
                base_match and _normalized_header(base_match.header) != _normalized_header(chunk.header)
            )
            if changed_signature:
                signature_changed_names.add(chunk.name)

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
                try:
                    caller_source = (root / hit.path).read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                caller_chunks = parse_file(caller_source, hit.path)
                caller_chunk = None
                for c in caller_chunks:
                    if c.start_line <= hit.line <= c.end_line:
                        caller_chunk = c
                        break
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

    # A changed function that also calls the changed symbol would otherwise
    # appear twice — once as changed code, once as its own caller — spending
    # twice the tokens on one function. The changed block already contains the
    # call site, so the caller copy adds nothing but cost, and the waste grows
    # with a PR whose changed functions call each other.
    for span, block in caller_candidates:
        if span in changed_spans:
            continue
        candidates.append(block)

    # Pack by priority: must-include first, then tier, then declaration order
    # (stable sort preserves that). Degrade optional callers to header-only
    # before dropping them outright; must-include blocks are never dropped —
    # the honesty principle is "budget can shrink detail, never hide the fact
    # that this code exists."
    candidates.sort(key=lambda b: (not b.must_include, b.tier))

    ctx = ReviewContext()
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
