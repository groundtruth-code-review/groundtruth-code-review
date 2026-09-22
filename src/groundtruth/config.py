"""Load `.groundtruth.yml` — and enforce the one hard rule about it: this
file can never hold a key. That's not a style preference, it's what makes
the file safe to commit at all. A config that *could* hold a secret is a
config someone eventually commits by accident; a config that structurally
can't is one nobody has to remember not to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_DIMENSIONS = ["correctness", "security", "conventions"]

# Best-effort, not a secret scanner: common key prefixes (sk-... covers
# OpenAI/Anthropic-style keys) or a long opaque alphanumeric run. False
# positives are the acceptable failure mode here — the config simply
# refuses to load and says why, which is a config edit, not a security
# incident. False negatives are why keys-from-env is the actual rule this
# backstops, not the rule itself.
_KEY_LIKE_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{10,}|[A-Za-z0-9_-]{32,})\b")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    model: str = "anthropic/claude-sonnet-5"
    verify_model: str | None = None
    summary_model: str | None = None
    llm_base_url: str | None = None
    max_cost_per_run: float | None = None
    min_confidence: float = 0.7
    max_inline_comments: int = 10
    context_token_budget: int = 25_000
    max_diff_tokens_per_call: int = 6_000
    summary: bool = True
    dimensions: list[str] = field(default_factory=lambda: list(_DEFAULT_DIMENSIONS))

    @property
    def review_model(self) -> str:
        return self.model

    @property
    def resolved_verify_model(self) -> str:
        """The skeptic pass reads one finding against its evidence, so a
        cheaper model is often enough — but defaulting to a *different* model
        than the user configured would change both cost and behaviour behind
        their back. Unset means "the same model", and splitting the tiers is
        something the user opts into.
        """
        return self.verify_model or self.model

    @property
    def resolved_summary_model(self) -> str:
        return self.summary_model or self.verify_model or self.model


def _reject_embedded_keys(data: dict, source: str) -> None:
    for key, value in data.items():
        if isinstance(value, str) and _KEY_LIKE_RE.search(value):
            raise ConfigError(
                f"'{key}' in {source} looks like it contains a live API key. "
                "Keys are environment variables, always — remove this value "
                "and set the matching env var instead (see .env.example)."
            )


def load_config(path: Path | str | None) -> Config:
    defaults = Config()
    if path is None:
        return defaults
    path = Path(path)
    if not path.exists():
        return defaults

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a YAML mapping, not a {type(raw).__name__}")
    _reject_embedded_keys(raw, str(path))

    return Config(
        model=raw.get("model", defaults.model),
        verify_model=raw.get("verify_model", defaults.verify_model),
        summary_model=raw.get("summary_model", defaults.summary_model),
        llm_base_url=raw.get("llm_base_url", defaults.llm_base_url),
        max_cost_per_run=raw.get("max_cost_per_run", defaults.max_cost_per_run),
        min_confidence=raw.get("min_confidence", defaults.min_confidence),
        max_inline_comments=raw.get("max_inline_comments", defaults.max_inline_comments),
        context_token_budget=raw.get("context_token_budget", defaults.context_token_budget),
        max_diff_tokens_per_call=raw.get("max_diff_tokens_per_call", defaults.max_diff_tokens_per_call),
        summary=raw.get("summary", defaults.summary),
        dimensions=raw.get("dimensions", list(_DEFAULT_DIMENSIONS)),
    )
