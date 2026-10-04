"""The optional, self-hosted server: a database behind the pipeline.

Nothing here runs unless someone deploys it. `groundtruth review` and the
three CI adapters work exactly as before, with or without this package
installed -- it is a fourth, optional front door, not a replacement for the
other three, and it never runs `context_engine` or `quality_gate` itself.

Scope: phase 1 of docs/server-mode-design.md -- ingest only. A CI adapter
that has `GROUNDTRUTH_INGEST_URL` set POSTs its JSON output here after
posting to the pull request, and this stores it. Nothing yet reads it back
out (no dashboard endpoint, no feedback ingestion) -- that is phase 2 and
3 of the same design doc, not started.

Install with `pip install "groundtruth-review[server]"`; the base package
never needs FastAPI, uvicorn or psycopg.
"""
