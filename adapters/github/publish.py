#!/usr/bin/env python3
"""The GitHub front door: run the review, then post it to the pull request.

One upserted summary comment plus one inline comment per finding. The text
of both, and the fingerprint marker that remembers what was already posted,
live in `adapters/common.py` so that GitLab and Bitbucket say the same
things; what stays here is GitHub's API and nothing else.

Deliberately stdlib-only (no `requests`, no dependency on the `groundtruth`
package itself). This script speaks one API and reads the plain JSON
contract `groundtruth review --format json` prints; it needs to know nothing
about context engines or quality gates to do that.

`GitHubClient` and `main()` are the thin, mostly-untestable part that talks
to api.github.com, kept as small as possible on purpose.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (  # noqa: E402
    FINGERPRINT_PREFIX,
    SUMMARY_MARKER,
    has_marker,
    inline_comment_body,
    parse_fingerprint_marker,
    render_fingerprint_marker,
    render_summary,
    run_groundtruth_review,
)

__all__ = [
    "FINGERPRINT_PREFIX",
    "SUMMARY_MARKER",
    "has_marker",
    "inline_comment_body",
    "parse_fingerprint_marker",
    "render_fingerprint_marker",
    "render_summary",
    "run_groundtruth_review",
    "GitHubClient",
    "GitHubApiError",
    "inline_comment_payloads",
    "find_previous_summary",
    "upsert_summary",
    "main",
]


def inline_comment_payloads(outcome: dict, commit_id: str) -> list[dict]:
    payloads = []
    for f in outcome.get("findings", []):
        payloads.append({
            "commit_id": commit_id,
            "path": f["file"],
            "line": f["line"],
            "body": inline_comment_body(f),
            "side": "RIGHT",
        })
    return payloads


class GitHubApiError(RuntimeError):
    pass


class GitHubClient:
    """As thin as it can be: one `_request` method, four call sites. No
    retry/backoff logic here on purpose — a transient failure to post a
    comment should be visible in the Action's own logs, not silently masked.
    """

    def __init__(self, token: str, repo: str, opener=urllib.request.urlopen):
        self.token = token
        self.repo = repo
        self._opener = opener

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"https://api.github.com{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "groundtruth-review-action",
            },
        )
        try:
            with self._opener(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitHubApiError(f"{method} {path} -> {exc.code}: {detail}") from exc

    def list_issue_comments(self, pr_number: int) -> list[dict]:
        return self._request("GET", f"/repos/{self.repo}/issues/{pr_number}/comments") or []

    def create_issue_comment(self, pr_number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{self.repo}/issues/{pr_number}/comments", {"body": body})

    def update_issue_comment(self, comment_id: int, body: str) -> dict:
        return self._request("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}", {"body": body})

    def create_review_comment(self, pr_number: int, payload: dict) -> dict:
        return self._request("POST", f"/repos/{self.repo}/pulls/{pr_number}/comments", payload)


def find_previous_summary(client: GitHubClient, pr_number: int) -> dict | None:
    """This action's own last summary comment on the pull request, if any."""
    existing = client.list_issue_comments(pr_number)
    return next((c for c in existing if has_marker(c.get("body", ""))), None)


def upsert_summary(
    client: GitHubClient, pr_number: int, body: str, previous: dict | None = None
) -> None:
    """`previous` is passed in when the caller already looked it up, so one
    run does not list every comment on the pull request twice.
    """
    marker_comment = previous if previous is not None else find_previous_summary(client, pr_number)
    if not marker_comment:
        client.create_issue_comment(pr_number, body)
        return

    try:
        client.update_issue_comment(marker_comment["id"], body)
    except GitHubApiError:
        # The comment we meant to edit is gone, locked, or on a resolved
        # thread. Posting a fresh one costs a duplicate summary; letting
        # this raise would cost the whole review, because the inline
        # comments are posted after this call.
        client.create_issue_comment(pr_number, body)


def main() -> int:
    event_path = os.environ["GITHUB_EVENT_PATH"]
    token = os.environ["GH_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    model = os.environ.get("GROUNDTRUTH_MODEL") or None

    event = json.loads(Path(event_path).read_text())
    pr = event["pull_request"]
    base_sha = pr["base"]["sha"]
    head_sha = pr["head"]["sha"]
    pr_number = pr["number"]

    client = GitHubClient(token=token, repo=repo)

    # Read the memory before writing the review: findings already commented
    # on an earlier push are dropped by the gate instead of being posted a
    # second time. A fresh pull request simply has no marker to read.
    previous = find_previous_summary(client, pr_number)
    already_posted = parse_fingerprint_marker(previous.get("body", "")) if previous else []

    outcome = run_groundtruth_review(workspace, base_sha, head_sha, model, already_posted)

    # Everything posted so far, so the next push reads one cumulative list
    # rather than only this run's findings.
    outcome["fingerprints"] = sorted(set(already_posted) | set(outcome.get("fingerprints", [])))

    upsert_summary(client, pr_number, render_summary(outcome), previous=previous)

    for payload in inline_comment_payloads(outcome, commit_id=head_sha):
        try:
            client.create_review_comment(pr_number, payload)
        except GitHubApiError as exc:
            # One finding failing to post (e.g. its line isn't part of the
            # diff GitHub itself sees, which can happen after a force-push
            # race) shouldn't take down the whole review.
            print(f"warning: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
