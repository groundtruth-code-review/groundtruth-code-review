"""What every SCM adapter shares: the review's own text and its memory.

Each adapter speaks exactly one API. What none of them should own is the
*content* — the summary comment's markdown, the hidden fingerprint marker
that carries the review's memory to the next push, the body of an inline
comment. Written per adapter, those would drift apart and a fix to one
would quietly skip the others.

Stdlib only, like the adapters themselves: this module never imports
`groundtruth`. An adapter's job is to move a diff in and comments out, and
it should keep running on nothing but a Python interpreter.
"""

from __future__ import annotations

import json
import subprocess

SUMMARY_MARKER = "<!-- groundtruth-review:summary -->"
FINGERPRINT_PREFIX = "<!-- groundtruth-review:fingerprints "


def render_fingerprint_marker(fingerprints: list[str]) -> str:
    """The review's memory, carried in its own comment.

    A finding's fingerprint has to survive to the next push or the same
    comment gets posted again, and the cheapest durable store available to
    a stateless CI job is the comment it already upserts: an HTML comment is
    invisible in the rendered body, travels with the pull request, and needs
    no database, no cache and no credentials beyond the token already in
    hand. It is also honest about its limits — someone who deletes the
    summary comment resets the memory, which costs a duplicate comment and
    nothing worse.
    """
    if not fingerprints:
        return ""
    return FINGERPRINT_PREFIX + " ".join(sorted(fingerprints)) + " -->"


def parse_fingerprint_marker(comment_body: str) -> list[str]:
    """Read back what a previous run posted. Anything unparseable returns
    nothing, so a mangled comment means "no memory" rather than a crash.
    """
    body = comment_body or ""
    start = body.find(FINGERPRINT_PREFIX)
    if start == -1:
        return []
    end = body.find("-->", start)
    if end == -1:
        return []
    return body[start + len(FINGERPRINT_PREFIX):end].split()


def _meta_line(outcome: dict) -> str:
    """Model and cost as one quiet line — useful, but not what a reviewer
    opening the PR is here to read.
    """
    bits = []
    if outcome.get("model"):
        bits.append(f"`{outcome['model']}`")
    cost = outcome.get("estimated_cost_usd")
    if cost is not None:
        bits.append(f"estimated ${cost:.4f}")
    return " · ".join(bits)


def render_summary(outcome: dict) -> str:
    """One comment, scannable top to bottom: a count, then the findings
    grouped by file in the gate's own ranking order, then the quiet
    metadata. Grouping by file matters because that is how a reviewer reads
    a PR — three findings in one file is a different problem from one
    finding in three files.
    """
    findings = outcome.get("findings", [])
    lines = [SUMMARY_MARKER, "## Groundtruth review", ""]

    if outcome.get("message") and not findings:
        lines.append(outcome["message"])
        return "\n".join(lines)

    if not findings:
        lines.append("No findings survived the quality gate.")
    else:
        count = len(findings)
        if outcome.get("summary"):
            lines.append(outcome["summary"])
            lines.append("")
        lines.append(f"**{count} verified finding{'s' if count != 1 else ''}**, grouped by file.")
        lines.append("")

        by_file: dict[str, list[dict]] = {}
        for f in findings:
            by_file.setdefault(f["file"], []).append(f)

        for path, group in by_file.items():
            lines.append(f"#### `{path}`")
            lines.append("")
            lines.append("| Severity | Line | Finding | Confidence |")
            lines.append("| --- | --- | --- | --- |")
            for f in group:
                lines.append(
                    f"| **{f['severity'].upper()}** | {f['line']} "
                    f"| {f['title']} <br><sub>{f['category']}</sub> "
                    f"| {f['confidence']:.2f} |"
                )
            lines.append("")
        lines.append("Every row also has its own inline comment on the line it names.")
        lines.append("")

    footer = []
    dropped = outcome.get("dropped_count", 0)
    if dropped:
        footer.append(f"{dropped} candidate finding(s) did not survive verification.")
    meta = _meta_line(outcome)
    if meta:
        footer.append(f"Reviewed with {meta}.")
    if footer:
        lines.append(f"<sub>{' '.join(footer)}</sub>")

    marker = render_fingerprint_marker(outcome.get("fingerprints", []))
    if marker:
        lines.extend(["", marker])

    return "\n".join(lines)


def has_marker(comment_body: str) -> bool:
    return SUMMARY_MARKER in (comment_body or "")


def inline_comment_body(finding: dict) -> str:
    """The text of one comment on one line, identical on every platform.

    A fenced block rather than a blockquote, and the quote keeps its leading
    indentation: how deeply a line is nested is often part of what the
    finding is about. Only stray newlines and trailing spaces come off.
    """
    return (
        f"**{finding['severity'].upper()}** \u00b7 {finding['category']} "
        f"\u00b7 confidence {finding['confidence']:.2f}\n\n"
        f"{finding['title']}\n\n"
        f"```\n{finding['quoted_code'].strip(chr(10)).rstrip()}\n```"
    )


def run_groundtruth_review(
    workspace: str, base_sha: str, head_sha: str, model: str | None,
    seen_fingerprints: list[str] | None = None,
) -> dict:
    cmd = [
        "groundtruth", "review", "--repo", workspace,
        "--base", base_sha, "--head", head_sha, "--format", "json",
    ]
    if model:
        cmd += ["--model", model]
    for fingerprint in seen_fingerprints or []:
        cmd += ["--seen-fingerprint", fingerprint]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"groundtruth review failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


