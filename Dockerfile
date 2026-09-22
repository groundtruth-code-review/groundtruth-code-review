# syntax=docker/dockerfile:1
#
# A failing test suite cannot produce a shippable image.
#
# The runtime stage installs a wheel that only exists if the `test` stage
# built it, and that stage builds it only after ruff, both test suites and
# the eval thresholds have passed in the same layer chain. There is no
# `--skip-tests` path: skipping them means having no wheel to install.

FROM python:3.11-slim AS base

# git because the review shells out to it for diffs and file versions, and
# ripgrep because the caller search is dramatically faster with it (there is
# a pure-Python fallback, but an image may as well carry the fast path).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*


# Pinned to the builder's own architecture. The gate and the wheel it
# produces are architecture-independent (the package is pure Python), so
# running them once natively is enough -- without this pin, a multi-platform
# release runs the whole suite again under arm64 emulation, which turns a
# two-minute gate into a long one for no added confidence.
FROM --platform=$BUILDPLATFORM base AS test

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY tests ./tests
COPY adapters ./adapters
COPY cases ./cases

RUN pip install --no-cache-dir ".[dev]" build

# The gate. Offline: every suite runs against fake model clients, and the
# eval cases carry recorded responses, so no key and no network are needed
# to prove the pipeline still works.
RUN ruff check . \
    && pytest -q \
    && pytest -q adapters \
    && groundtruth eval --cases cases --min-catch 0.6 --max-fp 0.2

RUN python -m build --wheel --outdir /wheels


FROM base AS runtime

COPY --from=test /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# CI mounts a checkout owned by whatever uid the runner used, and git refuses
# to operate on a repository owned by a different user ("detected dubious
# ownership"). The container only ever reads the repository it was pointed at,
# so trusting mounted paths here is the narrow fix; the alternative is every
# user discovering the error themselves.
RUN git config --system --add safe.directory '*'

# Nothing here needs root: the review reads a working tree and writes JSON to
# stdout. Running as root would only widen what a compromised model response
# could reach.
RUN useradd --create-home --uid 10001 reviewer
USER reviewer

WORKDIR /workspace
ENTRYPOINT ["groundtruth"]
CMD ["review", "--help"]
