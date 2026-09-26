import os

import pytest

psycopg = pytest.importorskip("psycopg")

POSTED = {
    "file": "invoice.py",
    "line": 1,
    "category": "correctness",
    "severity": "high",
    "title": "New promos argument is required, but checkout.py still calls calculate_discount(price)",
    "quoted_code": "def calculate_discount(price, promos):",
    "confidence": 0.81,
    "fingerprint": "fp-posted-1",
}

DROPPED = {
    "file": "invoice.py",
    "line": 2,
    "category": "correctness",
    "severity": "medium",
    "title": "Empty promos list is not handled",
    "quoted_code": "rate = 1 - sum(p.rate for p in promos or [])",
    "confidence": None,
    "fingerprint": "fp-dropped-1",
    "dropped_at": "hallucination",
    "reason": "quoted_code was not found in the diff or assembled context",
}


def _payload(**overrides) -> dict:
    payload = {
        "platform": "github",
        "repo": "acme/widgets",
        "pr_number": "42",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "started_at": "2026-09-26T10:00:00Z",
        "finished_at": "2026-09-26T10:00:05Z",
        "outcome": {
            "findings": [POSTED],
            "dropped": [DROPPED],
            "summary": "One required argument was added without updating its caller.",
            "review_incomplete": False,
            "review_calls": 1,
            "review_failures": 0,
            "verify_failures": 0,
            "model": "anthropic/claude-sonnet-5",
            "estimated_cost_usd": 0.0032,
        },
    }
    payload.update(overrides)
    return payload


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def test_healthz_reports_ok_when_the_database_answers(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ingest_stores_the_review_and_every_finding_posted_or_dropped(client, conn):
    resp = client.post("/reviews", json=_payload())
    assert resp.status_code == 200
    review_id = resp.json()["id"]

    reviews = _rows(conn, "SELECT * FROM reviews WHERE id = %s", (review_id,))
    assert len(reviews) == 1
    review = reviews[0]
    assert review["repo"] == "acme/widgets"
    assert review["model"] == "anthropic/claude-sonnet-5"
    assert float(review["estimated_cost_usd"]) == pytest.approx(0.0032)
    assert review["review_incomplete"] is False

    findings = _rows(conn, "SELECT * FROM findings WHERE review_id = %s ORDER BY line", (review_id,))
    assert len(findings) == 2
    assert findings[0]["posted"] is True
    assert findings[0]["dropped_at"] is None
    assert findings[1]["posted"] is False
    assert findings[1]["dropped_at"] == "hallucination"
    assert findings[1]["dropped_reason"] == "quoted_code was not found in the diff or assembled context"


def test_a_second_ingest_of_the_same_commit_replaces_it_rather_than_duplicating(client, conn):
    # A retried POST or a re-triggered CI run for a commit already ingested
    # must not double-count that review on a dashboard.
    client.post("/reviews", json=_payload())
    second = client.post("/reviews", json=_payload(pr_number="43"))
    assert second.status_code == 200

    reviews = _rows(conn, "SELECT * FROM reviews WHERE repo = 'acme/widgets'")
    assert len(reviews) == 1
    assert reviews[0]["pr_number"] == "43"


def test_a_finding_the_model_stops_proposing_does_not_linger_after_a_re_ingest(client, conn):
    first = _payload()
    resp = client.post("/reviews", json=first)
    review_id = resp.json()["id"]

    only_one_finding = _payload(outcome={**first["outcome"], "findings": [POSTED], "dropped": []})
    client.post("/reviews", json=only_one_finding)

    findings = _rows(conn, "SELECT * FROM findings WHERE review_id = %s", (review_id,))
    assert len(findings) == 1
    assert findings[0]["fingerprint"] == "fp-posted-1"


def test_a_malformed_payload_is_rejected_before_it_reaches_the_database(client):
    resp = client.post("/reviews", json={"platform": "github"})
    assert resp.status_code == 422


def test_a_configured_token_is_required(client, monkeypatch):
    monkeypatch.setenv("GROUNDTRUTH_INGEST_TOKEN", "s3cret")
    try:
        assert client.post("/reviews", json=_payload()).status_code == 401
        bad = client.post("/reviews", json=_payload(), headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
        good = client.post("/reviews", json=_payload(), headers={"Authorization": "Bearer s3cret"})
        assert good.status_code == 200
    finally:
        monkeypatch.delenv("GROUNDTRUTH_INGEST_TOKEN", raising=False)


def test_no_token_configured_means_no_auth_required(client, monkeypatch):
    monkeypatch.delenv("GROUNDTRUTH_INGEST_TOKEN", raising=False)
    assert os.environ.get("GROUNDTRUTH_INGEST_TOKEN") is None
    resp = client.post("/reviews", json=_payload())
    assert resp.status_code == 200
