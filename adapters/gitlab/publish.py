#!/usr/bin/env python3
"""The GitLab front door: run the review, then post it to the merge request.

Same shape as the GitHub adapter, because the core command is the same:
fetch nothing, run `groundtruth review`, post what it printed. Only the API
differs, and the differences are worth naming since they shaped this file:

  * GitLab calls them merge requests, and its own CI variables
    (`CI_MERGE_REQUEST_IID`, `CI_MERGE_REQUEST_DIFF_BASE_SHA`) carry
    everything needed, so there is no event payload to read.
  * A comment on a line is a *discussion* with a `position`, and that
    position needs both SHAs plus the path on both sides of the diff.
  * A plain comment is a *note*. Editing one needs its id, which is how the
    summary is upserted rather than duplicated on every push.

Stdlib only, and it never imports `groundtruth`: everything it says lives in
`adapters/common.py`, everything it knows about GitLab lives here.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (  # noqa: E402
    has_marker,
    inline_comment_body,
    maybe_ingest,
    parse_finding_marker,
    parse_fingerprint_marker,
    render_summary,
    run_groundtruth_review,
)


class GitLabApiError(RuntimeError):
    pass


def discussion_payloads(outcome: dict, base_sha: str, head_sha: str) -> list[dict]:
    """One positioned discussion per finding.

    `old_path` is set to the same value as `new_path` because a finding is
    always about a line that exists on the head side — the gate drops any
    finding whose line is not inside a changed hunk, so there is no case
    here where only the old side exists.
    """
    payloads = []
    for finding in outcome.get("findings", []):
        payloads.append({
            "body": inline_comment_body(finding),
            "position[position_type]": "text",
            "position[base_sha]": base_sha,
            "position[start_sha]": base_sha,
            "position[head_sha]": head_sha,
            "position[new_path]": finding["file"],
            "position[old_path]": finding["file"],
            "position[new_line]": str(finding["line"]),
        })
    return payloads


class GitLabClient:
    """One `_request` method, four call sites, no retry logic: a transient
    failure should be visible in the job log, not masked.
    """

    def __init__(self, token: str, project_id: str, api_url: str = "https://gitlab.com/api/v4",
                 opener=urllib.request.urlopen):
        self.token = token
        self.project = urllib.parse.quote(str(project_id), safe="")
        self.api_url = api_url.rstrip("/")
        self._opener = opener

    def _request(self, method: str, path: str, body: dict | None = None, form: bool = False):
        url = f"{self.api_url}{path}"
        headers = {
            "PRIVATE-TOKEN": self.token,
            "User-Agent": "groundtruth-review-gitlab",
        }
        data = None
        if body is not None and form:
            data = urllib.parse.urlencode(body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitLabApiError(f"{method} {path} -> {exc.code}: {detail}") from exc

    def _paginate(self, path: str, per_page: int = 100, max_pages: int = 10) -> list[dict]:
        """Every item, not just the first page. GitLab returns 20 by default, and a
        merge request collects system notes ("added 3 commits") with every push, so
        crossing 20 is ordinary -- and past it the summary this action posted earlier
        is on a page it never read, so it would post a second one and forget which
        findings it had already said.
        """
        items: list[dict] = []
        joiner = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            batch = self._request("GET", f"{path}{joiner}per_page={per_page}&page={page}") or []
            items.extend(batch)
            if len(batch) < per_page:
                break
        return items

    def list_notes(self, mr_iid: str) -> list[dict]:
        return self._paginate(f"/projects/{self.project}/merge_requests/{mr_iid}/notes")

    def list_discussions(self, mr_iid: str) -> list[dict]:
        return self._paginate(f"/projects/{self.project}/merge_requests/{mr_iid}/discussions")

    def list_award_emoji(self, mr_iid: str, note_id: int) -> list[dict]:
        return self._paginate(
            f"/projects/{self.project}/merge_requests/{mr_iid}/notes/{note_id}/award_emoji"
        )

    def create_note(self, mr_iid: str, body: str) -> dict:
        return self._request(
            "POST", f"/projects/{self.project}/merge_requests/{mr_iid}/notes", {"body": body}
        )

    def update_note(self, mr_iid: str, note_id: int, body: str) -> dict:
        return self._request(
            "PUT", f"/projects/{self.project}/merge_requests/{mr_iid}/notes/{note_id}", {"body": body}
        )

    def create_discussion(self, mr_iid: str, payload: dict) -> dict:
        return self._request(
            "POST", f"/projects/{self.project}/merge_requests/{mr_iid}/discussions", payload, form=True
        )


def collect_feedback(client: GitLabClient, mr_iid: str) -> list[dict]:
    """What people did with this job's earlier inline discussions: a thumbs up or
    down, or resolving the thread. Each entry is about one finding, by fingerprint.

    Best effort and read-only, like the GitHub adapter's: a failure to read it costs
    this run's feedback report, never the review.
    """
    entries: list[dict] = []
    try:
        for discussion in client.list_discussions(mr_iid):
            notes = discussion.get("notes") or []
            first = notes[0] if notes else {}
            fingerprint = parse_finding_marker(first.get("body", ""))
            if not fingerprint:
                continue
            counts: dict[str, int] = {}
            for award in client.list_award_emoji(mr_iid, first["id"]):
                if award.get("name") in ("thumbsup", "thumbsdown"):
                    counts[award["name"]] = counts.get(award["name"], 0) + 1
            for name, count in counts.items():
                entries.append(
                    {
                        "fingerprint": fingerprint,
                        "kind": "reaction",
                        "source": f"gitlab:{name}",
                        "count": count,
                    }
                )
            if first.get("resolved") is True:
                entries.append(
                    {"fingerprint": fingerprint, "kind": "resolved", "source": "gitlab:resolved", "count": 1}
                )
    except GitLabApiError as exc:
        print(f"warning: could not read feedback on earlier discussions: {exc}", file=sys.stderr)
        return []
    return entries


def find_previous_summary(client: GitLabClient, mr_iid: str) -> dict | None:
    return next((n for n in client.list_notes(mr_iid) if has_marker(n.get("body", ""))), None)


def upsert_summary(client: GitLabClient, mr_iid: str, body: str, previous: dict | None = None) -> None:
    note = previous if previous is not None else find_previous_summary(client, mr_iid)
    if note:
        client.update_note(mr_iid, note["id"], body)
    else:
        client.create_note(mr_iid, body)


def main() -> int:
    token = os.environ["GROUNDTRUTH_GITLAB_TOKEN"]
    project_id = os.environ["CI_PROJECT_ID"]
    mr_iid = os.environ["CI_MERGE_REQUEST_IID"]
    base_sha = os.environ["CI_MERGE_REQUEST_DIFF_BASE_SHA"]
    head_sha = os.environ.get("CI_COMMIT_SHA", "HEAD")
    workspace = os.environ.get("CI_PROJECT_DIR", ".")
    api_url = os.environ.get("CI_API_V4_URL", "https://gitlab.com/api/v4")
    model = os.environ.get("GROUNDTRUTH_MODEL") or None

    client = GitLabClient(token=token, project_id=project_id, api_url=api_url)

    previous = find_previous_summary(client, mr_iid)
    already_posted = parse_fingerprint_marker(previous.get("body", "")) if previous else []

    started_at = datetime.now(timezone.utc)
    outcome = run_groundtruth_review(workspace, base_sha, head_sha, model, already_posted)
    finished_at = datetime.now(timezone.utc)
    outcome["fingerprints"] = sorted(set(already_posted) | set(outcome.get("fingerprints", [])))

    upsert_summary(client, mr_iid, render_summary(outcome), previous=previous)

    for payload in discussion_payloads(outcome, base_sha, head_sha):
        try:
            client.create_discussion(mr_iid, payload)
        except GitLabApiError as exc:
            # One finding failing to place (a force-push race, a line GitLab
            # does not consider part of the diff) must not take the review
            # down with it.
            print(f"warning: {exc}", file=sys.stderr)

    maybe_ingest(
        outcome,
        platform="gitlab",
        repo=os.environ.get("CI_PROJECT_PATH", project_id),
        pr_number=str(mr_iid),
        base_sha=base_sha,
        head_sha=head_sha,
        started_at=started_at,
        finished_at=finished_at,
        feedback=collect_feedback(client, mr_iid) if os.environ.get("GROUNDTRUTH_INGEST_URL") else None,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
