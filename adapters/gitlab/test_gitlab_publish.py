"""    python3 -m pytest adapters/gitlab/test_gitlab_publish.py"""

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
_spec = importlib.util.spec_from_file_location("gitlab_publish", _ADAPTER_DIR / "publish.py")
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)

GitLabApiError = publish.GitLabApiError
GitLabClient = publish.GitLabClient
discussion_payloads = publish.discussion_payloads
find_previous_summary = publish.find_previous_summary
upsert_summary = publish.upsert_summary

FINDING = {
    "file": "invoice.py", "line": 2, "category": "correctness", "severity": "high",
    "title": "Discount can exceed 100%", "quoted_code": "    rate = sum(p.rate for p in promos)",
    "confidence": 0.86, "fingerprint": "abc123",
}


class FakeOpener:
    """Records requests and replays queued responses, so the client's URLs,
    methods and bodies are asserted without a network.
    """

    def __init__(self, responses=None):
        self.requests = []
        self._responses = list(responses or [])

    def __call__(self, req):
        body = req.data.decode() if req.data else None
        self.requests.append((req.method, req.full_url, body, dict(req.headers)))
        payload = self._responses.pop(0) if self._responses else {}

        class Response:
            def read(self_inner):
                return json.dumps(payload).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

        return Response()


def _client(opener):
    return GitLabClient(token="glpat-x", project_id="group/app", opener=opener)


def test_the_project_path_is_url_encoded():
    opener = FakeOpener([[]])
    _client(opener).list_notes("7")
    assert "/projects/group%2Fapp/merge_requests/7/notes" in opener.requests[0][1]


def test_a_discussion_carries_both_shas_and_the_line():
    payloads = discussion_payloads({"findings": [FINDING]}, base_sha="b1", head_sha="h9")
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["position[base_sha]"] == "b1"
    assert payload["position[head_sha]"] == "h9"
    assert payload["position[new_path]"] == "invoice.py"
    assert payload["position[new_line]"] == "2"
    assert "Discount can exceed 100%" in payload["body"]


def test_a_discussion_is_form_encoded_because_positions_are_bracketed_keys():
    opener = FakeOpener([{}])
    payload = discussion_payloads({"findings": [FINDING]}, "b1", "h9")[0]
    _client(opener).create_discussion("7", payload)
    method, url, body, headers = opener.requests[0]
    assert method == "POST"
    assert headers["Content-type"] == "application/x-www-form-urlencoded"
    assert "position%5Bnew_line%5D=2" in body


def test_no_findings_means_no_discussions():
    assert discussion_payloads({"findings": []}, "b", "h") == []


def test_the_summary_is_edited_not_duplicated():
    marked = {"id": 42, "body": "<!-- groundtruth-review:summary -->\n## Groundtruth review"}
    opener = FakeOpener([[{"id": 1, "body": "unrelated"}, marked], {}])
    client = _client(opener)
    previous = find_previous_summary(client, "7")
    assert previous["id"] == 42
    upsert_summary(client, "7", "new body", previous=previous)
    method, url, _, _ = opener.requests[1]
    assert method == "PUT"
    assert url.endswith("/notes/42")


def test_a_first_run_creates_the_summary():
    opener = FakeOpener([[{"id": 1, "body": "unrelated"}], {}])
    client = _client(opener)
    upsert_summary(client, "7", "first body")
    assert [r[0] for r in opener.requests] == ["GET", "POST"]


def test_the_token_goes_in_the_private_token_header():
    opener = FakeOpener([[]])
    _client(opener).list_notes("7")
    assert opener.requests[0][3]["Private-token"] == "glpat-x"


def test_an_http_error_names_the_call():
    import urllib.error

    def failing(req):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    try:
        GitLabClient(token="t", project_id="p", opener=failing).list_notes("7")
    except GitLabApiError as exc:
        assert "403" in str(exc)
    else:
        raise AssertionError("an API failure must not pass silently")
