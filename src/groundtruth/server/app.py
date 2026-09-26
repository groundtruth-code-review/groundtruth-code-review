"""The ingest API: `POST /reviews`, and nothing that reads the data back
out yet -- see the module docstring in `__init__.py` for what this is.

Run it with `uvicorn groundtruth.server.app:app`. `GROUNDTRUTH_DB_URL` (or
`DATABASE_URL`) has to be set before it will start; there is no default,
on purpose (see `db.dsn_from_env`).
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status

from . import db
from .models import IngestRequest

logger = logging.getLogger(__name__)

ENV_TOKEN = "GROUNDTRUTH_INGEST_TOKEN"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="groundtruth-review ingest",
    description="Optional, self-hosted: stores what `groundtruth review` already "
    "printed. See docs/server-mode-design.md.",
    lifespan=lifespan,
)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Checked only when `GROUNDTRUTH_INGEST_TOKEN` is set. Unset, this
    endpoint accepts unauthenticated writes -- documented in
    docs/server-mode-design.md as a decision to make deliberately, not a
    default to leave in place on anything reachable from the internet.
    """
    expected = os.environ.get(ENV_TOKEN)
    if not expected:
        return
    got = (authorization or "").removeprefix("Bearer ").strip()
    # constant-time: a bearer token is a secret, and a plain `!=` leaks how
    # many leading characters matched through response timing
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")


@app.get("/healthz")
def healthz() -> dict:
    """Verifies the database, not just that the process is up -- a health
    check that only proves it can answer HTTP is proving the least
    interesting thing about this service.
    """
    try:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            conn.close()
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"database unreachable: {exc}") from exc
    return {"status": "ok"}


@app.post("/reviews", dependencies=[Depends(require_token)])
def ingest(req: IngestRequest) -> dict:
    conn = db.connect()
    try:
        review_id = db.ingest_review(conn, req)
    finally:
        conn.close()
    return {"status": "ok", "id": review_id}
