"""Server tests need a real Postgres, not a mock -- the whole point is to
prove the SQL, including the upsert-and-replace behaviour, is correct.
Skips cleanly (not a failure) when either the `server` extra isn't
installed or no test database is configured, so a bare `pytest -q` on the
core package is unaffected either way.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

TEST_DB_URL_VAR = "GROUNDTRUTH_TEST_DB_URL"


@pytest.fixture(scope="session")
def db_dsn():
    dsn = os.environ.get(TEST_DB_URL_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DB_URL_VAR} not set -- server tests need a real Postgres to run against")
    return dsn


@pytest.fixture
def conn(db_dsn, monkeypatch):
    monkeypatch.setenv("GROUNDTRUTH_DB_URL", db_dsn)
    from groundtruth.server import db

    connection = db.connect()
    db.init_db(connection)
    yield connection
    with connection.cursor() as cur:
        cur.execute("TRUNCATE reviews, findings, feedback RESTART IDENTITY CASCADE")
    connection.commit()
    connection.close()


@pytest.fixture
def client(conn):
    from fastapi.testclient import TestClient

    from groundtruth.server.app import app

    with TestClient(app) as c:
        yield c
