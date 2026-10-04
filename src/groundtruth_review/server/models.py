"""What an ingest request has to contain, and nothing about how it's stored.

These mirror two shapes that already exist elsewhere and are not free to
drift from them: `FindingEntry` is `cli._verdict_to_dict`'s output, and
`Outcome` is `cli.render_json`'s payload. `extra="allow"` on `Outcome`
because a field this schema doesn't name yet (added to the CLI's JSON
later) should still ingest -- it lands in `raw_outcome` either way -- and
a stricter model would make every CLI-side addition a breaking change
here too.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FindingEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str
    line: int
    category: str
    severity: str
    title: str
    quoted_code: str
    confidence: float | None = None
    fingerprint: str
    dropped_at: str | None = None
    reason: str | None = None


class Outcome(BaseModel):
    model_config = ConfigDict(extra="allow")

    findings: list[FindingEntry] = Field(default_factory=list)
    dropped: list[FindingEntry] = Field(default_factory=list)
    summary: str | None = None
    review_incomplete: bool = False
    review_calls: int = 0
    review_failures: int = 0
    verify_failures: int = 0
    model: str | None = None
    verify_model: str | None = None
    summary_model: str | None = None
    estimated_cost_usd: float | None = None


class IngestRequest(BaseModel):
    """What a CI adapter POSTs to `/reviews` after a run, if
    `GROUNDTRUTH_INGEST_URL` is set. The adapter supplies the platform
    context the CLI's own JSON has no way to know (repo, PR number,
    platform name); everything else is that JSON, structured.
    """

    platform: str
    repo: str
    pr_number: str
    base_sha: str
    head_sha: str
    started_at: datetime
    finished_at: datetime
    outcome: Outcome
