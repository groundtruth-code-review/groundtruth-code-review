"""Everything that touches the database, in one place.

One connection per call, opened and closed around the work -- no pool yet.
An ingest endpoint answers one POST per CI run, not one per end user
request; a pool is worth adding when real traffic says it's worth adding,
not before there is any.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .models import IngestRequest

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

ENV_DB_URL = "GROUNDTRUTH_DB_URL"


class DbNotConfigured(RuntimeError):
    pass


def dsn_from_env() -> str:
    """`GROUNDTRUTH_DB_URL`, falling back to the conventional
    `DATABASE_URL` most Postgres hosts already set. Never a default: a
    server with no database configured should fail loudly at startup, not
    guess at a local connection nobody intended.
    """
    dsn = os.environ.get(ENV_DB_URL) or os.environ.get("DATABASE_URL")
    if not dsn:
        raise DbNotConfigured(
            f"No database configured. Set {ENV_DB_URL} (or DATABASE_URL) to a "
            "Postgres connection string, e.g. postgresql://user:pass@host/dbname"
        )
    return dsn


def connect() -> psycopg.Connection:
    return psycopg.connect(dsn_from_env())


def init_db(conn: psycopg.Connection) -> None:
    """Apply schema.sql. Every statement in it is `IF NOT EXISTS`, so this
    is safe to call on every startup against a database that already has
    the schema -- there is no separate migration step to remember to run.
    """
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_PATH.read_text())
    conn.commit()


def _finding_rows(review_id: int, req: IngestRequest) -> list[tuple]:
    rows = [
        (
            review_id,
            f.fingerprint,
            f.file,
            f.line,
            f.category,
            f.severity,
            f.confidence,
            f.title,
            f.quoted_code,
            True,
            None,
            None,
            f.proof,
        )
        for f in req.outcome.findings
    ]
    rows += [
        (
            review_id,
            f.fingerprint,
            f.file,
            f.line,
            f.category,
            f.severity,
            f.confidence,
            f.title,
            f.quoted_code,
            False,
            f.dropped_at,
            f.reason,
            f.proof,
        )
        for f in req.outcome.dropped
    ]
    return rows


@dataclass(frozen=True)
class Ingested:
    review_id: int
    feedback_recorded: int


_UPSERT_FEEDBACK = """
    INSERT INTO feedback (finding_id, kind, source, count)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (finding_id, kind, source) DO UPDATE SET count = EXCLUDED.count, updated_at = now()
"""


def _record_feedback(cur, req: IngestRequest) -> int:
    """File each reported reaction under the latest posted finding it is about.

    A reaction is reported on a later run than the one that posted the
    finding, so the finding is looked up by fingerprint across every review of
    this pull request. One the server never saw (it was not running when that
    comment was posted) is skipped rather than invented.

    Only ever adds or raises what is stored: a reaction someone later removes
    is simply not reported again, so a stale count can outlive it. That is the
    safe direction to be wrong in -- a failed read of the pull request must not
    be able to erase feedback.
    """
    recorded = 0
    for entry in req.feedback:
        cur.execute(
            """
            SELECT f.id FROM findings f JOIN reviews r ON r.id = f.review_id
            WHERE r.platform = %s AND r.repo = %s AND r.pr_number = %s
              AND f.fingerprint = %s AND f.posted
            ORDER BY f.id DESC LIMIT 1
            """,
            (req.platform, req.repo, req.pr_number, entry.fingerprint),
        )
        row = cur.fetchone()
        if row is None:
            continue
        cur.execute(_UPSERT_FEEDBACK, (row[0], entry.kind, entry.source, entry.count))
        recorded += 1
    return recorded


def ingest_review(conn: psycopg.Connection, req: IngestRequest) -> Ingested:
    """Write one review, its findings and any feedback it reports.

    Upserts on (platform, repo, head_sha): a retried POST or a re-triggered
    CI run for the same commit updates the existing row and replaces its
    findings, rather than accumulating a second row that would double-count
    the same review in anything reading this table.
    """
    outcome = req.outcome
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reviews (
                platform, repo, pr_number, base_sha, head_sha,
                model, verify_model, summary_model,
                started_at, finished_at,
                review_incomplete, review_calls, review_failures, verify_failures,
                estimated_cost_usd, raw_outcome, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, now()
            )
            ON CONFLICT (platform, repo, head_sha) DO UPDATE SET
                pr_number          = EXCLUDED.pr_number,
                base_sha           = EXCLUDED.base_sha,
                model              = EXCLUDED.model,
                verify_model       = EXCLUDED.verify_model,
                summary_model      = EXCLUDED.summary_model,
                started_at         = EXCLUDED.started_at,
                finished_at        = EXCLUDED.finished_at,
                review_incomplete  = EXCLUDED.review_incomplete,
                review_calls       = EXCLUDED.review_calls,
                review_failures    = EXCLUDED.review_failures,
                verify_failures    = EXCLUDED.verify_failures,
                estimated_cost_usd = EXCLUDED.estimated_cost_usd,
                raw_outcome        = EXCLUDED.raw_outcome,
                updated_at         = now()
            RETURNING id
            """,
            (
                req.platform,
                req.repo,
                req.pr_number,
                req.base_sha,
                req.head_sha,
                outcome.model,
                outcome.verify_model,
                outcome.summary_model,
                req.started_at,
                req.finished_at,
                outcome.review_incomplete,
                outcome.review_calls,
                outcome.review_failures,
                outcome.verify_failures,
                outcome.estimated_cost_usd,
                outcome.model_dump_json(),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        review_id = row[0]

        # Re-ingesting the same commit replaces its findings outright
        # rather than merging: a finding the model no longer proposes on a
        # later run should not linger here as a stale row. Feedback hangs off
        # those rows and would go with them, so it is set aside first and put
        # back on the matching posted finding afterwards.
        cur.execute(
            """
            SELECT f.fingerprint, b.kind, b.source, b.count
            FROM feedback b JOIN findings f ON f.id = b.finding_id
            WHERE f.review_id = %s
            """,
            (review_id,),
        )
        kept = cur.fetchall()
        cur.execute("DELETE FROM findings WHERE review_id = %s", (review_id,))
        rows = _finding_rows(review_id, req)
        if rows:
            cur.executemany(
                """
                INSERT INTO findings (
                    review_id, fingerprint, file, line, category, severity,
                    confidence, title, quoted_code, posted, dropped_at, dropped_reason, proof
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
        for fingerprint, kind, source, count in kept:
            cur.execute(
                "SELECT id FROM findings WHERE review_id = %s AND fingerprint = %s AND posted "
                "ORDER BY id LIMIT 1",
                (review_id, fingerprint),
            )
            row = cur.fetchone()
            if row is not None:
                cur.execute(_UPSERT_FEEDBACK, (row[0], kind, source, count))
        recorded = _record_feedback(cur, req)
    conn.commit()
    return Ingested(review_id=review_id, feedback_recorded=recorded)


_THUMBS_UP = "('+1', 'thumbsup')"
_THUMBS_DOWN = "('-1', 'thumbsdown')"


def repo_stats(conn: psycopg.Connection, repo: str, days: int) -> dict:
    """What the stored reviews and the feedback on them add up to, for one repo.

    Counted per finding, not per reaction: `thumbs_down` is how many posted
    findings got at least one thumbs-down, so dividing it by `posted` is a rate
    that means something. Small numbers say little; this reports, it doesn't
    advise.
    """
    window = "r.started_at >= now() - make_interval(days => %s)"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*), count(*) FILTER (WHERE review_incomplete) FROM reviews r "
            f"WHERE r.repo = %s AND {window}",
            (repo, days),
        )
        total, incomplete = cur.fetchone()

        cur.execute(
            f"""
            SELECT f.posted, coalesce(f.dropped_at, ''), (coalesce(f.proof, '') <> ''), count(*)
            FROM findings f JOIN reviews r ON r.id = f.review_id
            WHERE r.repo = %s AND {window}
            GROUP BY 1, 2, 3
            """,
            (repo, days),
        )
        posted = proven = 0
        dropped: dict[str, int] = {}
        for was_posted, stage, is_proven, n in cur.fetchall():
            if was_posted:
                posted += n
                proven += n if is_proven else 0
            else:
                dropped[stage or "ranked_out"] = dropped.get(stage or "ranked_out", 0) + n

        def reacted(signals: str) -> str:
            return (
                "count(*) FILTER (WHERE f.posted AND EXISTS (SELECT 1 FROM feedback b "
                "WHERE b.finding_id = f.id AND b.kind = 'reaction' "
                f"AND split_part(b.source, ':', 2) IN {signals}))"
            )

        cur.execute(
            f"""
            SELECT f.category,
                   count(*) FILTER (WHERE f.posted),
                   {reacted(_THUMBS_UP)},
                   {reacted(_THUMBS_DOWN)},
                   count(*) FILTER (WHERE f.posted AND EXISTS (SELECT 1 FROM feedback b
                       WHERE b.finding_id = f.id AND b.kind = 'resolved'))
            FROM findings f JOIN reviews r ON r.id = f.review_id
            WHERE r.repo = %s AND {window}
            GROUP BY f.category ORDER BY f.category
            """,
            (repo, days),
        )
        by_category = {
            category: {"posted": p, "thumbs_up": up, "thumbs_down": down, "resolved": res}
            for category, p, up, down, res in cur.fetchall()
            if p
        }
    return {
        "repo": repo,
        "days": days,
        "reviews": {"total": total, "incomplete": incomplete},
        "findings": {"posted": posted, "proven": proven, "dropped": dropped},
        "by_category": by_category,
    }
