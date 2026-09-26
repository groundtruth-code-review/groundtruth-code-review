"""Shared test setup.

Endpoints are read from the environment, so a developer who exported
GROUNDTRUTH_BASE_URL in their own shell would otherwise change what these
tests see. Every test starts with those variables unset; a test that needs
one sets it explicitly with monkeypatch.
"""

import pytest

_ENDPOINT_VARS = (
    "GROUNDTRUTH_BASE_URL",
    "GROUNDTRUTH_VERIFY_BASE_URL",
    "GROUNDTRUTH_SUMMARY_BASE_URL",
)


@pytest.fixture(autouse=True)
def _no_endpoints_from_the_developers_shell(monkeypatch):
    for name in _ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)
