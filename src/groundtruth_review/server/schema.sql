-- Groundtruth server mode: phase 1 (ingest only). See
-- docs/server-mode-design.md for what this is and isn't.
--
-- Postgres, not SQLite: the eventual webhook receiver (phase 2, not built)
-- writes from more than one worker at a time, and a dashboard is exactly
-- the kind of ad-hoc querying Postgres is built for. Applied idempotently
-- by init_db() -- every statement is safe to run against a database that
-- already has this schema.

-- One row per ingested run of `groundtruth review`. A second ingest for
-- the same (platform, repo, head_sha) replaces this row rather than
-- appending -- a retried POST or a re-triggered CI run is the same review,
-- not a second one.
CREATE TABLE IF NOT EXISTS reviews (
    id                BIGSERIAL PRIMARY KEY,
    platform          TEXT NOT NULL,
    repo              TEXT NOT NULL,
    pr_number         TEXT NOT NULL,
    base_sha          TEXT NOT NULL,
    head_sha          TEXT NOT NULL,
    model             TEXT,
    verify_model      TEXT,
    summary_model     TEXT,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ NOT NULL,
    review_incomplete BOOLEAN NOT NULL DEFAULT FALSE,
    review_calls      INTEGER NOT NULL DEFAULT 0,
    review_failures   INTEGER NOT NULL DEFAULT 0,
    verify_failures   INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd NUMERIC(10, 6),
    -- The exact JSON `groundtruth review --format json` printed. Kept
    -- whole alongside the columns above so a field this schema doesn't
    -- surface yet can be backfilled without re-ingesting.
    raw_outcome       JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reviews_platform_repo_head_sha_key UNIQUE (platform, repo, head_sha)
);

CREATE INDEX IF NOT EXISTS idx_reviews_repo_started
    ON reviews (repo, started_at DESC);

-- One row per candidate the gate saw, posted or not. Re-ingesting a
-- review (see the unique constraint above) deletes and reinserts this
-- review's findings, so a fingerprint the model stops proposing on a
-- later re-run doesn't linger here as a stale row.
CREATE TABLE IF NOT EXISTS findings (
    id             BIGSERIAL PRIMARY KEY,
    review_id      BIGINT NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
    fingerprint    TEXT NOT NULL,
    file           TEXT NOT NULL,
    line           INTEGER NOT NULL,
    category       TEXT NOT NULL,
    severity       TEXT NOT NULL,
    confidence     NUMERIC(4, 3),
    title          TEXT NOT NULL,
    quoted_code    TEXT NOT NULL,
    posted         BOOLEAN NOT NULL,
    -- Null for a posted finding; a gate stage name ("hallucination",
    -- "bad_location", "dedupe", "low_confidence", "ranked_out") otherwise.
    dropped_at     TEXT,
    dropped_reason TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_findings_review ON findings (review_id);
CREATE INDEX IF NOT EXISTS idx_findings_fingerprint ON findings (fingerprint);

-- Nothing writes here yet. This is phase 3 of server-mode-design.md: a
-- reaction, a resolved-without-comment thread, a dismissed suggestion,
-- keyed to the finding it was about. Created now so that phase is an
-- ingest path against an existing table, not a migration against a
-- database people are already depending on.
CREATE TABLE IF NOT EXISTS feedback (
    id         BIGSERIAL PRIMARY KEY,
    finding_id BIGINT NOT NULL REFERENCES findings (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_feedback_finding ON feedback (finding_id);
