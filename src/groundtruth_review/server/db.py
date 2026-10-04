"""Everything that touches the database, in one place.

One connection per call, opened and closed around the work -- no pool yet.
An ingest endpoint answers one POST per CI run, not one per end user
request; a pool is worth adding when real traffic says it's worth adding,
not before there is any.
"""

from __future__ import annotations

import os
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
        )
        for f in req.outcome.dropped
    ]
    return rows


def ingest_review(conn: psycopg.Connection, req: IngestRequest) -> int:
    """Write one review and its findings; return the review's id.

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
        # later run should not linger here as a stale row.
        cur.execute("DELETE FROM findings WHERE review_id = %s", (review_id,))
        rows = _finding_rows(review_id, req)
        if rows:
            cur.executemany(
                """
                INSERT INTO findings (
                    review_id, fingerprint, file, line, category, severity,
                    confidence, title, quoted_code, posted, dropped_at, dropped_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
    conn.commit()
    return review_id
