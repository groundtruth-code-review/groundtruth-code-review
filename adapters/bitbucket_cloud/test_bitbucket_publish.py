"""    python3 -m pytest adapters/bitbucket_cloud/test_bitbucket_publish.py"""

import base64
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
_spec = importlib.util.spec_from_file_location("bitbucket_publish", _ADAPTER_DIR / "publish.py")
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)

API_ROOT = publish.API_ROOT
BitbucketApiError = publish.BitbucketApiError
BitbucketClient = publish.BitbucketClient
comment_payloads = publish.comment_payloads
find_previous_summary = publish.find_previous_summary
upsert_summary = publish.upsert_summary

FINDING = {
    "file": "invoice.py", "line": 2, "category": "correctness", "severity": "high",
    "title": "Discount can exceed 100%", "quoted_code": "    rate = sum(p.rate for p in promos)",
    "confidence": 0.86, "fingerprint": "abc123",
}
MARKED = {"id": 99, "content": {"raw": "<!-- groundtruth-review:summary -->\n## Groundtruth review"}}


class FakeOpener:
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
    return BitbucketClient(
        user="bot", app_password="secret", workspace_repo="team/app", opener=opener
    )


def test_an_inline_comment_anchors_to_the_new_side_line():
    payloads = comment_payloads({"findings": [FINDING]})
    assert payloads[0]["inline"] == {"path": "invoice.py", "to": 2}
    assert "Discount can exceed 100%" in payloads[0]["content"]["raw"]


def test_a_summary_comment_has_no_inline_anchor():
    opener = FakeOpener([{}])
    _client(opener).create_comment("5", {"content": {"raw": "summary"}})
    body = json.loads(opener.requests[0][2])
    assert "inline" not in body


def test_credentials_go_out_as_basic_auth():
    opener = FakeOpener([{"values": []}])
    _client(opener).list_comments("5")
    expected = base64.b64encode(b"bot:secret").decode()
    assert opener.requests[0][3]["Authorization"] == f"Basic {expected}"


def test_every_page_of_comments_is_read():
    # the summary comment can sit on any page of a busy pull request
    page_one = {"values": [{"id": 1, "content": {"raw": "chat"}}],
                "next": f"{API_ROOT}/repositories/team/app/pullrequests/5/comments?page=2"}
    page_two = {"values": [MARKED]}
    opener = FakeOpener([page_one, page_two])
    found = find_previous_summary(_client(opener), "5")
    assert found["id"] == 99
    assert len(opener.requests) == 2


def test_the_summary_is_edited_not_duplicated():
    opener = FakeOpener([{"values": [MARKED]}, {}])
    client = _client(opener)
    upsert_summary(client, "5", "new body")
    method, url, body, _ = opener.requests[1]
    assert method == "PUT"
    assert url.endswith("/comments/99")
    assert json.loads(body)["content"]["raw"] == "new body"


def test_a_first_run_creates_the_summary():
    opener = FakeOpener([{"values": []}, {}])
    upsert_summary(_client(opener), "5", "first body")
    assert [r[0] for r in opener.requests] == ["GET", "POST"]


def test_an_http_error_names_the_call():
    import urllib.error

    def failing(req):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    try:
        BitbucketClient("u", "p", "team/app", opener=failing).list_comments("5")
    except BitbucketApiError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("an API failure must not pass silently")
