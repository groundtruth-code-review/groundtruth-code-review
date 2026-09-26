"""`groundtruth-server`, or `python -m groundtruth.server`: runs the ingest
API. `GROUNDTRUTH_DB_URL` must already be set -- see `db.dsn_from_env`.
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "groundtruth.server.app:app",
        host=os.environ.get("GROUNDTRUTH_SERVER_HOST", "0.0.0.0"),
        port=int(os.environ.get("GROUNDTRUTH_SERVER_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
