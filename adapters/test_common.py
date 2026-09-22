"""Tests for the text and the memory every adapter shares.

    python3 -m pytest adapters/test_common.py

Outside the main package's pytest run for the same reason the adapters are:
nothing here imports `groundtruth`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import (  # noqa: E402
    has_marker,
    inline_comment_body,
    parse_fingerprint_marker,
    render_fingerprint_marker,
    render_summary,
    run_groundtruth_review,
)

FINDING = {
    "file": "invoice.py", "line": 2, "category": "correctness", "severity": "high",
    "title": "Discount can exceed 100%", "quoted_code": "    rate = sum(p.rate for p in promos)",
    "confidence": 0.86, "fingerprint": "abc123",
}


def test_summary_groups_findings_under_their_file():
    body = render_summary({"findings": [FINDING, {**FINDING, "file": "checkout.py", "line": 9}],
                           "dropped_count": 0, "model": "m", "estimated_cost_usd": 0.02})
    assert "`invoice.py`" in body and "`checkout.py`" in body
    assert body.count("| Severity | Line | Finding | Confidence |") == 2


def test_summary_survives_a_missing_model_and_cost():
    body = render_summary({"findings": [FINDING], "dropped_count": 0})
    assert has_marker(body)
    assert "Reviewed with" not in body


def test_inline_body_keeps_the_quoted_code_in_a_fence():
    body = inline_comment_body(FINDING)
    assert body.startswith("**HIGH**")
    assert "```\n    rate = sum(p.rate for p in promos)\n```" in body


def test_fingerprints_round_trip_through_the_marker():
    body = render_summary({"findings": [FINDING], "fingerprints": ["a1", "b2"], "dropped_count": 0})
    assert parse_fingerprint_marker(body) == ["a1", "b2"]


def test_an_empty_fingerprint_list_writes_no_marker():
    assert render_fingerprint_marker([]) == ""
    assert parse_fingerprint_marker(render_summary({"findings": [], "dropped_count": 0})) == []


def test_seen_fingerprints_are_passed_to_the_cli():
    calls = {}

    def fake_run(cmd, capture_output, text):
        calls["cmd"] = cmd

        class R:
            returncode = 0
            stdout = '{"findings": [], "dropped_count": 0}'
            stderr = ""

        return R()

    import common
    original = common.subprocess.run
    common.subprocess.run = fake_run
    try:
        common.run_groundtruth_review("/ws", "base", "head", "openai/gpt-4o", ["abc123", "def456"])
    finally:
        common.subprocess.run = original

    cmd = calls["cmd"]
    assert cmd.count("--seen-fingerprint") == 2
    assert "abc123" in cmd and "def456" in cmd
    assert cmd[cmd.index("--model") + 1] == "openai/gpt-4o"


def test_a_failing_cli_run_raises_with_its_stderr():
    def fake_run(cmd, capture_output, text):
        class R:
            returncode = 1
            stdout = ""
            stderr = "error: no such ref"

        return R()

    import common
    original = common.subprocess.run
    common.subprocess.run = fake_run
    try:
        try:
            run_groundtruth_review("/ws", "base", "head", None)
        except RuntimeError as exc:
            assert "no such ref" in str(exc)
        else:
            raise AssertionError("a failed review must not pass silently")
    finally:
        common.subprocess.run = original
