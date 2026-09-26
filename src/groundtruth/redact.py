"""Describe a failure without leaking a secret into a log.

When a model call fails, the useful thing to log is why -- "authentication
failed", "model not found" -- and the provider's own message usually says.
But those messages sometimes echo part of the key back, and CI logs on a
public repository are public. So the message is cut to its first line, cut
to a readable length, and anything key-shaped is replaced before it is
written anywhere.
"""

from __future__ import annotations

import re

# Deliberately broad: a false positive costs one redacted word in a log
# line; a false negative costs a key.
_KEY_LIKE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|nvapi-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}"
    r"|AKIA[0-9A-Z]{12,}|Bearer\s+\S+|[A-Za-z0-9_-]{32,})"
)
_MAX_LEN = 240


def redact(text: str) -> str:
    return _KEY_LIKE.sub("[redacted]", text)


def describe_error(exc: BaseException) -> str:
    """`AuthenticationError: the api key is invalid` -- class name, then the
    first line of the message, redacted and trimmed."""
    lines = str(exc).strip().splitlines()
    first = next((line.strip() for line in lines if line.strip()), "")
    text = f"{type(exc).__name__}: {first}" if first else type(exc).__name__
    text = redact(text)
    return text if len(text) <= _MAX_LEN else text[: _MAX_LEN - 3] + "..."
