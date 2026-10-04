"""Standalone test file for the standalone publish.py — run directly:

    python3 -m pytest adapters/github/test_publish.py

It's not part of the main package's pytest run on purpose: publish.py has no
dependency on `groundtruth` itself (see its module docstring for why), so
its tests don't need that package importable either.
"""

import importlib.util
import json
import sys
from pathlib import Path

# Each adapter has its own `publish.py`, so a plain `import publish` would
# resolve to whichever adapter's copy a pytest session imported first. Load
# this one by path, under its own module name, and the three suites stay
# independent of collection order.
_ADAPTER_DIR = Path(__file__).parent
sys.path.insert(0, str(_ADAPTER_DIR.parent))
_spec = importlib.util.spec_from_file_location("github_publish", _ADAPTER_DIR / "publish.py")
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)

GitHubApiError = publish.GitHubApiError
GitHubClient = publish.GitHubClient
has_marker = publish.has_marker
inline_comment_payloads = publish.inline_comment_payloads
parse_fingerprint_marker = publish.parse_fingerprint_marker
render_summary = publish.render_summary
upsert_summary = publish.upsert_summary
find_previous_summary = publish.find_previous_summary

FINDING = {
    "file": "invoice.py",
    "line": 2,
    "category": "correctness",
    "severity": "high",
    "title": "Discount can exceed 100%",
    "quoted_code": "rate = sum(p.rate for p in promos)",
    "confidence": 0.86,
    "fingerprint": "abc123",
}


def test_summary_with_no_findings():
    body = render_summary({"findings": [], "dropped_count": 0, "estimated_cost_usd": 0.01, "model": "x"})
    assert "No findings survived" in body
    assert has_marker(body)


def test_summary_lists_findings_with_severity_and_location():
    outcome = {
        "findings": [FINDING], "dropped_count": 0,
        "estimated_cost_usd": 0.02, "model": "anthropic/claude-sonnet-5",
    }
    body = render_summary(outcome)
    assert "HIGH" in body
    assert "`invoice.py`" in body          # grouped under a per-file heading
    assert "| 2 " in body                  # its line, in the table's Line column
    assert "Discount can exceed 100%" in body
    assert "0.86" in body


def test_summary_mentions_dropped_count_when_nonzero():
    outcome = {"findings": [], "dropped_count": 3, "estimated_cost_usd": None, "model": None}
    body = render_summary(outcome)
    assert "3 candidate finding(s)" in body


def test_summary_omits_dropped_line_when_zero():
    outcome = {"findings": [], "dropped_count": 0, "estimated_cost_usd": None, "model": None}
    assert "candidate finding" not in render_summary(outcome)


def test_no_changes_message_is_shown_as_is():
    outcome = {
        "findings": [], "dropped_count": 0, "message": "no changes to review",
        "estimated_cost_usd": None, "model": None,
    }
    assert "no changes to review" in render_summary(outcome)


def test_has_marker_true_and_false():
    body = render_summary({"findings": [], "dropped_count": 0, "estimated_cost_usd": None, "model": None})
    assert has_marker(body) is True
    assert has_marker("some unrelated comment") is False
    assert has_marker("") is False


def test_inline_comment_payloads_shape():
    payloads = inline_comment_payloads({"findings": [FINDING]}, commit_id="deadbeef")
    assert len(payloads) == 1
    p = payloads[0]
    assert p["path"] == "invoice.py"
    assert p["line"] == 2
    assert p["commit_id"] == "deadbeef"
    assert p["side"] == "RIGHT"
    assert "Discount can exceed 100%" in p["body"]
    assert "```" in p["body"]  # the quoted code reads as a code block, not a blockquote


def test_inline_comment_payloads_empty_when_no_findings():
    assert inline_comment_payloads({"findings": []}, commit_id="x") == []


class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records every request made and returns a scripted response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def __call__(self, req):
        self.requests.append((req.get_method(), req.full_url))
        return FakeHTTPResponse(self._responses.pop(0))


def test_client_lists_comments_via_get():
    opener = FakeOpener([[{"id": 1, "body": "unrelated"}]])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    comments = client.list_issue_comments(42)
    assert comments == [{"id": 1, "body": "unrelated"}]
    assert opener.requests[0] == (
        "GET", "https://api.github.com/repos/me/repo/issues/42/comments?per_page=100&page=1"
    )


def test_upsert_creates_when_no_marker_comment_exists():
    opener = FakeOpener([[{"id": 1, "body": "unrelated"}], {"id": 2}])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    upsert_summary(client, 42, "new body")
    methods = [r[0] for r in opener.requests]
    assert methods == ["GET", "POST"]


def test_upsert_updates_the_existing_marked_comment_instead_of_creating():
    empty_outcome = {"findings": [], "dropped_count": 0, "estimated_cost_usd": None, "model": None}
    marked_body = render_summary(empty_outcome)
    opener = FakeOpener([[{"id": 99, "body": marked_body}], {"id": 99}])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    upsert_summary(client, 42, "updated body")
    methods_and_urls = [(r[0], r[1]) for r in opener.requests]
    assert methods_and_urls[0][0] == "GET"
    assert methods_and_urls[1] == ("PATCH", "https://api.github.com/repos/me/repo/issues/comments/99")


def test_client_raises_a_readable_error_on_http_failure():
    import io
    import urllib.error

    def raising_opener(req):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", hdrs=None, fp=io.BytesIO(b"not found")
        )

    client = GitHubClient(token="t", repo="me/repo", opener=raising_opener)
    try:
        client.list_issue_comments(1)
        raise AssertionError("expected GitHubApiError")
    except GitHubApiError as exc:
        assert "404" in str(exc)


# --- the review's memory between pushes ----------------------------------

def test_fingerprint_marker_round_trips():
    body = render_summary({"findings": [FINDING], "fingerprints": ["abc123", "def456"],
                           "dropped_count": 0, "model": "m", "estimated_cost_usd": 0.01})
    assert parse_fingerprint_marker(body) == ["abc123", "def456"]


def test_fingerprint_marker_is_invisible_in_the_rendered_body():
    body = render_summary({"findings": [FINDING], "fingerprints": ["abc123"], "dropped_count": 0})
    # an HTML comment, so a reader sees nothing of it
    assert "<!-- groundtruth-review:fingerprints abc123 -->" in body
    assert "abc123" not in body.replace("<!-- groundtruth-review:fingerprints abc123 -->", "")


def test_no_marker_means_no_memory_rather_than_an_error():
    assert parse_fingerprint_marker("just a normal comment") == []
    assert parse_fingerprint_marker("") == []
    assert parse_fingerprint_marker("<!-- groundtruth-review:fingerprints abc") == []


def test_summary_text_from_the_model_leads_the_comment():
    body = render_summary({
        "findings": [FINDING], "summary": "One contract break in the invoice path.",
        "dropped_count": 0, "model": "m", "estimated_cost_usd": 0.01,
    })
    assert body.index("One contract break in the invoice path.") < body.index("1 verified finding")


def test_a_deleted_summary_comment_does_not_kill_the_review():
    # the inline comments are posted after the summary, so an unguarded
    # failure here used to take the whole review down with it
    calls = []

    class Client:
        def update_issue_comment(self, comment_id, body):
            calls.append(("PATCH", comment_id))
            raise publish.GitHubApiError("PATCH -> 404: Not Found")

        def create_issue_comment(self, pr_number, body):
            calls.append(("POST", pr_number))
            return {"id": 7}

    upsert_summary(Client(), 42, "body", previous={"id": 99, "body": "old"})

    assert calls == [("PATCH", 99), ("POST", 42)]


# ------------------------------------------------ reading what people did with our comments


def test_a_long_comment_list_is_read_in_full_not_just_the_first_page():
    # GitHub returns 30 by default; a busy pull request used to hide this action's
    # own summary from it, so it posted a second one and forgot what it had said
    page_one = [{"id": i, "body": "x"} for i in range(100)]
    page_two = [{"id": 100, "body": "last"}]
    opener = FakeOpener([page_one, page_two])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    comments = client.list_issue_comments(42)
    assert len(comments) == 101 and comments[-1]["body"] == "last"
    assert [url.rsplit("?", 1)[1] for _, url in opener.requests] == [
        "per_page=100&page=1", "per_page=100&page=2",
    ]


def test_a_short_first_page_stops_the_listing():
    opener = FakeOpener([[{"id": 1, "body": "only"}]])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    client.list_review_comments(7)
    assert len(opener.requests) == 1
    assert opener.requests[0][1].startswith("https://api.github.com/repos/me/repo/pulls/7/comments?")


def _thread(comment_id, resolved):
    return {"isResolved": resolved, "comments": {"nodes": [{"databaseId": comment_id}]}}


def _threads(nodes, has_next=False, cursor=None):
    page = {"hasNextPage": has_next, "endCursor": cursor}
    data = {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": page, "nodes": nodes}}}}
    return {"data": data}


def test_resolved_threads_are_found_by_their_first_comments_id():
    opener = FakeOpener([_threads([_thread(11, True), _thread(12, False)])])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    assert client.resolved_comment_ids(5) == {11}
    assert opener.requests[0] == ("POST", "https://api.github.com/graphql")


def test_resolved_threads_follow_the_cursor_through_every_page():
    opener = FakeOpener([
        _threads([_thread(1, True)], has_next=True, cursor="c1"),
        _threads([_thread(2, True)]),
    ])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    assert client.resolved_comment_ids(5) == {1, 2}


def test_a_graphql_reply_without_data_means_no_resolved_threads_not_a_crash():
    opener = FakeOpener([{"errors": [{"message": "nope"}]}])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    assert client.resolved_comment_ids(5) == set()


def _ours(comment_id, fingerprint, **reactions):
    return {
        "id": comment_id,
        "body": f"**HIGH** · x\n\nT\n\n<!-- groundtruth-review:finding {fingerprint} -->",
        "reactions": {"+1": 0, "-1": 0, "heart": 5, **reactions},
    }


def test_thumbs_and_resolved_threads_are_reported_per_finding():
    comments = [_ours(1, "fp-a", **{"-1": 2}), _ours(2, "fp-b", **{"+1": 1}), _ours(3, "fp-c")]
    opener = FakeOpener([comments, _threads([_thread(3, True)])])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    assert publish.collect_feedback(client, 9) == [
        {"fingerprint": "fp-a", "kind": "reaction", "source": "github:-1", "count": 2},
        {"fingerprint": "fp-b", "kind": "reaction", "source": "github:+1", "count": 1},
        {"fingerprint": "fp-c", "kind": "resolved", "source": "github:resolved", "count": 1},
    ]


def test_other_reactions_and_other_peoples_comments_are_not_feedback():
    human = {"id": 5, "body": "looks good to me", "reactions": {"+1": 9, "-1": 0}}
    opener = FakeOpener([[human, _ours(1, "fp-a", heart=3)], _threads([])])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    assert publish.collect_feedback(client, 9) == []  # a heart is not a verdict; a human's comment isn't ours


def test_no_comments_of_ours_means_no_graphql_call_at_all():
    opener = FakeOpener([[{"id": 5, "body": "not ours", "reactions": {}}]])
    client = GitHubClient(token="t", repo="me/repo", opener=opener)
    assert publish.collect_feedback(client, 9) == []
    assert len(opener.requests) == 1


def test_a_failure_reading_feedback_costs_the_report_not_the_review():
    class Failing:
        def __call__(self, req):
            raise publish.urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)

    client = GitHubClient(token="t", repo="me/repo", opener=Failing())
    assert publish.collect_feedback(client, 9) == []
