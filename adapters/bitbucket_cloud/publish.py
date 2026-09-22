#!/usr/bin/env python3
"""The Bitbucket Cloud front door: run the review, post it to the PR.

Same core command as the other adapters; the platform differences that
shaped this file:

  * Both kinds of comment go to the *same* endpoint. A comment with an
    `inline` anchor lands on a line, one without lands on the pull request.
    So there is no separate "review comment" API to learn, only a payload
    shape.
  * Editing a comment is a PUT to that comment's own id, which is how the
    summary is upserted instead of duplicated on every push.
  * Pipelines exposes the PR id as `BITBUCKET_PR_ID` and the merge target as
    `BITBUCKET_PR_DESTINATION_BRANCH` — a branch name, not a SHA, so the
    review is asked to diff against `origin/<branch>`.
  * Authentication is an app password or a repository access token over
    Basic auth; there is no single-header equivalent of a GitHub token.

Stdlib only, and it never imports `groundtruth`.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (  # noqa: E402
    has_marker,
    inline_comment_body,
    parse_fingerprint_marker,
    render_summary,
    run_groundtruth_review,
)

API_ROOT = "https://api.bitbucket.org/2.0"


class BitbucketApiError(RuntimeError):
    pass


def comment_payloads(outcome: dict) -> list[dict]:
    """One anchored comment per finding. `to` is the line on the new side of
    the diff, which is where every posted finding sits by construction — the
    gate drops anything outside a changed hunk.
    """
    return [
        {
            "content": {"raw": inline_comment_body(finding)},
            "inline": {"path": finding["file"], "to": finding["line"]},
        }
        for finding in outcome.get("findings", [])
    ]


class BitbucketClient:
    def __init__(self, user: str, app_password: str, workspace_repo: str,
                 api_root: str = API_ROOT, opener=urllib.request.urlopen):
        self.workspace_repo = workspace_repo
        self.api_root = api_root.rstrip("/")
        self._auth = base64.b64encode(f"{user}:{app_password}".encode()).decode()
        self._opener = opener

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{self.api_root}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Basic {self._auth}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "groundtruth-review-bitbucket",
            },
        )
        try:
            with self._opener(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise BitbucketApiError(f"{method} {path} -> {exc.code}: {detail}") from exc

    def list_comments(self, pr_id: str) -> list[dict]:
        """Bitbucket paginates with a `next` URL. Only the summary comment is
        being looked for, but it can sit on any page of a busy pull request,
        so every page is read rather than just the first.
        """
        path = f"/repositories/{self.workspace_repo}/pullrequests/{pr_id}/comments?pagelen=100"
        comments: list[dict] = []
        while path:
            page = self._request("GET", path) or {}
            comments.extend(page.get("values", []))
            next_url = page.get("next")
            path = next_url[len(self.api_root):] if next_url and next_url.startswith(self.api_root) else ""
        return comments

    def create_comment(self, pr_id: str, payload: dict) -> dict:
        return self._request(
            "POST", f"/repositories/{self.workspace_repo}/pullrequests/{pr_id}/comments", payload
        )

    def update_comment(self, pr_id: str, comment_id: int, body: str) -> dict:
        return self._request(
            "PUT",
            f"/repositories/{self.workspace_repo}/pullrequests/{pr_id}/comments/{comment_id}",
            {"content": {"raw": body}},
        )


def find_previous_summary(client: BitbucketClient, pr_id: str) -> dict | None:
    for comment in client.list_comments(pr_id):
        if has_marker(comment.get("content", {}).get("raw", "")):
            return comment
    return None


def upsert_summary(client: BitbucketClient, pr_id: str, body: str, previous: dict | None = None) -> None:
    comment = previous if previous is not None else find_previous_summary(client, pr_id)
    if comment:
        client.update_comment(pr_id, comment["id"], body)
    else:
        client.create_comment(pr_id, {"content": {"raw": body}})


def main() -> int:
    user = os.environ["GROUNDTRUTH_BITBUCKET_USER"]
    app_password = os.environ["GROUNDTRUTH_BITBUCKET_APP_PASSWORD"]
    workspace_repo = os.environ["BITBUCKET_REPO_FULL_NAME"]
    pr_id = os.environ["BITBUCKET_PR_ID"]
    target_branch = os.environ.get("BITBUCKET_PR_DESTINATION_BRANCH", "main")
    head_sha = os.environ.get("BITBUCKET_COMMIT", "HEAD")
    workspace = os.environ.get("BITBUCKET_CLONE_DIR", ".")
    model = os.environ.get("GROUNDTRUTH_MODEL") or None

    client = BitbucketClient(user=user, app_password=app_password, workspace_repo=workspace_repo)

    previous = find_previous_summary(client, pr_id)
    already_posted = (
        parse_fingerprint_marker(previous.get("content", {}).get("raw", "")) if previous else []
    )

    # A branch name, not a SHA: Pipelines gives the merge target by name, and
    # the clone has it as a remote ref.
    outcome = run_groundtruth_review(
        workspace, f"origin/{target_branch}", head_sha, model, already_posted
    )
    outcome["fingerprints"] = sorted(set(already_posted) | set(outcome.get("fingerprints", [])))

    upsert_summary(client, pr_id, render_summary(outcome), previous=previous)

    for payload in comment_payloads(outcome):
        try:
            client.create_comment(pr_id, payload)
        except BitbucketApiError as exc:
            print(f"warning: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
