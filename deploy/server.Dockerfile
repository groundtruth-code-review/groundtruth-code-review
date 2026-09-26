# syntax=docker/dockerfile:1
#
# The optional server-mode ingest API (docs/server-mode-design.md). Separate
# from the top-level Dockerfile on purpose: that one builds the CLI image
# every CI adapter runs, and gates it by running the full test suite inside
# the build -- which this image can't do the same way, since tests/server
# needs a real, running Postgres that a Docker build has no service
# container to provide. CI proves this image's code with a real Postgres
# (the server-test job); this Dockerfile only packages what CI already
# proved.

FROM python:3.11-slim AS base

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir ".[server]"

RUN useradd --create-home --uid 10001 server
USER server

EXPOSE 8000
ENTRYPOINT ["groundtruth-server"]
