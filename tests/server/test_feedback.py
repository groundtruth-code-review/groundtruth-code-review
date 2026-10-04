import pytest

pytest.importorskip("psycopg")

from test_ingest import DROPPED, POSTED, _payload, _rows  # noqa: E402

DOWN = {"fingerprint": "fp-posted-1", "kind": "reaction", "source": "github:-1", "count": 2}
RESOLVED = {"fingerprint": "fp-posted-1", "kind": "resolved", "source": "github:resolved", "count": 1}


def first_run(client, **overrides):
    return client.post("/reviews", json=_payload(**overrides))


def later_run(client, feedback, **overrides):
    """A second push on the same pull request, reporting what happened to the first run's comments.

    It posts nothing new: the fingerprint memory drops anything already said, which is
    why this run has to report the reactions -- it is the one that can see them.
    """
    payload = _payload(**{"head_sha": "c" * 40, "feedback": feedback, **overrides})
    payload["outcome"]["findings"] = []
    payload["outcome"]["dropped"] = []
    return client.post("/reviews", json=payload)


def stored_feedback(conn):
    return _rows(
        conn,
        "SELECT f.fingerprint, b.kind, b.source, b.count FROM feedback b "
        "JOIN findings f ON f.id = b.finding_id ORDER BY b.kind, b.source",
    )


def test_feedback_is_filed_under_the_finding_from_the_earlier_run(client, conn):
    first_run(client)
    resp = later_run(client, [DOWN, RESOLVED])
    assert resp.json()["feedback_recorded"] == 2
    assert stored_feedback(conn) == [
        {"fingerprint": "fp-posted-1", "kind": "reaction", "source": "github:-1", "count": 2},
        {"fingerprint": "fp-posted-1", "kind": "resolved", "source": "github:resolved", "count": 1},
    ]


def test_feedback_about_a_finding_the_server_never_saw_is_skipped_not_invented(client, conn):
    first_run(client)
    stranger = {**DOWN, "fingerprint": "fp-from-before-the-server-existed"}
    assert later_run(client, [stranger]).json()["feedback_recorded"] == 0
    assert stored_feedback(conn) == []


def test_feedback_never_attaches_to_a_different_pull_requests_finding(client, conn):
    first_run(client, pr_number="42")
    # same fingerprint, reported on pull request 99: that is not this finding
    assert later_run(client, [DOWN], pr_number="99").json()["feedback_recorded"] == 0
    assert stored_feedback(conn) == []


def test_a_finding_that_was_dropped_cannot_receive_feedback(client, conn):
    # only a comment people could see can have been reacted to
    first_run(client)
    dropped = {**DOWN, "fingerprint": DROPPED["fingerprint"]}
    assert later_run(client, [dropped]).json()["feedback_recorded"] == 0


def test_reporting_the_same_reaction_again_updates_the_row_rather_than_adding_one(client, conn):
    first_run(client)
    later_run(client, [DOWN])
    later_run(client, [{**DOWN, "count": 3}], head_sha="d" * 40)
    rows = stored_feedback(conn)
    assert len(rows) == 1 and rows[0]["count"] == 3


def test_re_ingesting_the_same_commit_keeps_the_feedback_already_collected(client, conn):
    # a CI re-run replaces the review's findings; the reactions on them must survive that
    first_run(client)
    later_run(client, [DOWN])
    first_run(client)  # the original commit, ingested again
    assert [r["source"] for r in stored_feedback(conn)] == ["github:-1"]


def test_a_database_made_before_feedback_had_counts_is_upgraded_in_place(conn):
    from groundtruth_review.server import db

    with conn.cursor() as cur:
        cur.execute("DROP TABLE feedback")
        cur.execute(
            "CREATE TABLE feedback (id BIGSERIAL PRIMARY KEY, finding_id BIGINT NOT NULL "
            "REFERENCES findings (id) ON DELETE CASCADE, kind TEXT NOT NULL, source TEXT NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    conn.commit()
    db.init_db(conn)
    columns = {r["column_name"] for r in _rows(
        conn, "SELECT column_name FROM information_schema.columns WHERE table_name = 'feedback'")}
    assert {"count", "updated_at"} <= columns


# ------------------------------------------------------------------ stats


def test_stats_counts_what_was_posted_dropped_and_reacted_to(client):
    first_run(client)
    later_run(client, [DOWN, RESOLVED])
    stats = client.get("/stats", params={"repo": "acme/widgets"}).json()
    assert stats["reviews"] == {"total": 2, "incomplete": 0}
    assert stats["findings"]["posted"] == 1
    assert stats["findings"]["dropped"] == {"hallucination": 1}
    assert stats["by_category"]["correctness"] == {
        "posted": 1, "thumbs_up": 0, "thumbs_down": 1, "resolved": 1,
    }


def test_stats_counts_findings_not_individual_reactions(client):
    first_run(client)
    later_run(client, [{**DOWN, "count": 7}])  # seven people, one finding
    assert client.get("/stats", params={"repo": "acme/widgets"}).json()["by_category"]["correctness"][
        "thumbs_down"
    ] == 1


def test_stats_says_how_many_findings_the_parser_proved(client):
    proven = {**POSTED, "fingerprint": "fp-proven", "proof": "Checked by parsing, not by a model: x"}
    payload = _payload()
    payload["outcome"]["findings"] = [POSTED, proven]
    client.post("/reviews", json=payload)
    findings = client.get("/stats", params={"repo": "acme/widgets"}).json()["findings"]
    assert findings["posted"] == 2 and findings["proven"] == 1


def test_stats_for_a_repo_with_nothing_stored_is_zeros_not_an_error(client):
    stats = client.get("/stats", params={"repo": "nobody/nothing"}).json()
    assert stats["reviews"]["total"] == 0 and stats["by_category"] == {}


def test_stats_needs_a_repo_and_a_sane_window(client):
    assert client.get("/stats").status_code == 422
    assert client.get("/stats", params={"repo": "a/b", "days": 0}).status_code == 422


def test_stats_requires_the_token_when_one_is_configured(client, monkeypatch):
    monkeypatch.setenv("GROUNDTRUTH_INGEST_TOKEN", "s3cret")
    assert client.get("/stats", params={"repo": "a/b"}).status_code == 401
    ok = client.get("/stats", params={"repo": "a/b"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
