"""Load `.groundtruth.yml` — and enforce the two hard rules about it.

1. It can never hold a key. That is what makes the file safe to commit: a
   config that *could* hold a secret is one someone eventually commits by
   accident; a config that structurally can't is one nobody has to remember
   not to.

2. It can never say where a key is sent. An endpoint decides which server
   receives your API key along with your code, and this file lives in the
   repository under review -- so the pull request being reviewed can edit
   it. Review bots are commonly run on `pull_request_target` so they can
   comment on forks, which hands the job your secrets; a fork that pointed
   the endpoint at its own server would collect your key on the first call.
   Endpoints therefore come from the same place keys do: environment
   variables and CLI flags, set by whoever owns the key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
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


# Keys in the file that would decide where a request -- and its API key --
# is sent. Rejected by name rather than ignored, so a stale config fails
# loudly instead of quietly calling the wrong server.
_ENDPOINT_KEYS = ("llm_base_url", "base_url", "api_base", "verify_base_url", "summary_base_url")

# Environment variables an endpoint can be set from, per stage.
ENV_BASE_URL = "GROUNDTRUTH_BASE_URL"
ENV_VERIFY_BASE_URL = "GROUNDTRUTH_VERIFY_BASE_URL"
ENV_SUMMARY_BASE_URL = "GROUNDTRUTH_SUMMARY_BASE_URL"


class ConfigError(ValueError):
    pass


# Sampling settings a stage may set. An allowlist, not a pass-through: this
# file can be edited by the pull request under review, so an open mapping
# would let it smuggle api_base or extra headers through as "parameters".
# Each is bounded for the same reason -- a PR must not be able to make
# every call on your key enormous.
_PARAM_BOUNDS = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_tokens": (1, 65_536),
}


def _validate_params(params: object, key: str, source: str) -> dict:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ConfigError(
            f"'{key}' in {source} must be a mapping, e.g. {{temperature: 1, max_tokens: 16384}}"
        )

    clean = {}
    for name, value in params.items():
        if name == "stream":
            raise ConfigError(
                f"'stream' in {key} ({source}) is not supported. The review has to read the whole "
                "reply to check its JSON, so a streamed reply cannot be used -- and in CI nobody "
                "is watching the tokens arrive. Remove it; every call is made unstreamed."
            )
        if name not in _PARAM_BOUNDS:
            allowed = ", ".join(sorted(_PARAM_BOUNDS))
            raise ConfigError(f"'{name}' in {key} ({source}) is not a supported setting. Allowed: {allowed}.")
        low, high = _PARAM_BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"'{name}' in {key} ({source}) must be a number, not {value!r}")
        if name == "max_tokens" and not float(value).is_integer():
            raise ConfigError(f"'max_tokens' in {key} ({source}) must be a whole number, not {value!r}")
        if not low <= value <= high:
            raise ConfigError(f"'{name}' in {key} ({source}) must be between {low} and {high}, not {value}")
        clean[name] = int(value) if name == "max_tokens" else float(value)
    return clean


NVIDIA_CATALOG_URL = "https://integrate.api.nvidia.com/v1"


def check_model_matches_endpoint(model: str, base_url: str | None, stage: str) -> None:
    """Fail before the first call when a model name will be misread at its
    endpoint.

    NVIDIA's catalog ids already carry their vendor -- openai/gpt-oss-20b,
    moonshotai/kimi-k3 -- and LiteLLM reads the first path segment as the
    provider. So "openai/gpt-oss-20b" goes out as an OpenAI call: it reads
    OPENAI_API_KEY instead of NVIDIA_NIM_API_KEY, and sends the model as
    "gpt-oss-20b", which NVIDIA does not serve. Both failures surface as a
    provider error that names neither cause. The fix is always the same --
    put nvidia_nim/ in front of the full id -- so say exactly that.
    """
    if base_url == NVIDIA_CATALOG_URL and not model.startswith("nvidia_nim/"):
        raise ConfigError(
            f"The {stage} model '{model}' is sent to NVIDIA's API catalog but is not prefixed "
            "'nvidia_nim/'. NVIDIA's model ids already include their vendor, and LiteLLM reads "
            "that vendor as the provider -- so the call would go out as a different provider, "
            "with the wrong key and a shortened model name. "
            f"Use '{'nvidia_nim/' + model}'."
        )


def normalize_base_url(url: str | None) -> str | None:
    """Trim whitespace and a trailing slash, and treat empty as unset.

    The slash is not cosmetic. LiteLLM recognizes some hosted endpoints by
    exact string -- NVIDIA's API catalog at https://integrate.api.nvidia.com/v1
    is routed to its own provider and key only on that exact spelling. With
    a trailing slash the match fails, the request falls back to generic
    OpenAI handling, reads the wrong key, and fails with an authentication
    error that points nowhere near the actual cause.
    """
    if url is None:
        return None
    url = url.strip().rstrip("/")
    return url or None


@dataclass(frozen=True)
class Config:
    model: str = "anthropic/claude-sonnet-5"
    verify_model: str | None = None
    summary_model: str | None = None
    # Endpoints, per stage. Never read from the config file; see the module
    # docstring for why. Unset means "call the provider's own default".
    llm_base_url: str | None = None
    verify_base_url: str | None = None
    summary_base_url: str | None = None
    # Sampling settings, per stage. Unlike endpoints these may live in the
    # file -- they change how a model answers, not where your key goes.
    model_params: dict = field(default_factory=dict)
    verify_model_params: dict | None = None
    summary_model_params: dict | None = None
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

    # An endpoint belongs to the model it serves, so each one resolves along
    # the same path its stage's model does. The case this exists for: review
    # on Claude, verify on a model behind NVIDIA's endpoint, summary unset.
    # The summary inherits the verifier's *model*, so it must inherit the
    # verifier's *endpoint* too -- falling back to the review endpoint would
    # send an NVIDIA model name to Anthropic.

    @property
    def review_base_url(self) -> str | None:
        return self.llm_base_url

    @property
    def resolved_verify_base_url(self) -> str | None:
        return self.verify_base_url or self.llm_base_url

    @property
    def resolved_summary_base_url(self) -> str | None:
        if self.summary_base_url:
            return self.summary_base_url
        if self.summary_model:
            return self.llm_base_url
        return self.resolved_verify_base_url


    # Sampling settings belong to one model, so -- unlike endpoints, which
    # have a shared default because one proxy can serve every model -- they
    # are inherited only when the model itself is. Settings tuned for a
    # reasoning model (temperature 1, a large token budget) must not follow
    # a verifier that names a different model.

    @property
    def review_params(self) -> dict:
        return dict(self.model_params)

    @property
    def resolved_verify_params(self) -> dict:
        if self.verify_model_params is not None:
            return dict(self.verify_model_params)
        return self.review_params if self.verify_model is None else {}

    @property
    def resolved_summary_params(self) -> dict:
        if self.summary_model_params is not None:
            return dict(self.summary_model_params)
        return self.resolved_verify_params if self.summary_model is None else {}


    def check_models_match_endpoints(self) -> None:
        check_model_matches_endpoint(self.review_model, self.review_base_url, "review")
        check_model_matches_endpoint(self.resolved_verify_model, self.resolved_verify_base_url, "verify")
        check_model_matches_endpoint(self.resolved_summary_model, self.resolved_summary_base_url, "summary")


def endpoints_from_env(config: Config) -> Config:
    """Apply endpoints from the environment. Anything set here overrides
    nothing in the file -- the file cannot set endpoints -- and is itself
    overridden by CLI flags, which the caller applies afterwards.
    """
    return replace(
        config,
        llm_base_url=normalize_base_url(os.environ.get(ENV_BASE_URL)) or config.llm_base_url,
        verify_base_url=normalize_base_url(os.environ.get(ENV_VERIFY_BASE_URL)) or config.verify_base_url,
        summary_base_url=normalize_base_url(os.environ.get(ENV_SUMMARY_BASE_URL)) or config.summary_base_url,
    )


def _reject_endpoints(data: dict, source: str) -> None:
    for key in _ENDPOINT_KEYS:
        if key in data:
            raise ConfigError(
                f"'{key}' in {source} would set where your API key is sent, and this file can be "
                "changed by the pull request being reviewed. Set the endpoint where the key lives "
                f"instead: {ENV_BASE_URL} (or {ENV_VERIFY_BASE_URL} / {ENV_SUMMARY_BASE_URL}) "
                "in the environment, or --base-url on the command line."
            )


def _reject_embedded_keys(data: dict, source: str) -> None:
    for key, value in data.items():
        if isinstance(value, str) and _KEY_LIKE_RE.search(value):
            raise ConfigError(
                f"'{key}' in {source} looks like it contains a live API key. "
                "Keys are environment variables, always — remove this value "
                "and set the matching env var instead (see .env.example)."
            )


def load_config(path: Path | str | None) -> Config:
    """Read the file, then the environment. The environment is applied even
    when there is no file at all, so an endpoint set in CI works for a
    repository that never added a `.groundtruth.yml`.
    """
    defaults = Config()
    if path is None:
        return endpoints_from_env(defaults)
    path = Path(path)
    if not path.exists():
        return endpoints_from_env(defaults)

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a YAML mapping, not a {type(raw).__name__}")
    _reject_embedded_keys(raw, str(path))
    _reject_endpoints(raw, str(path))

    config = Config(
        model=raw.get("model", defaults.model),
        verify_model=raw.get("verify_model", defaults.verify_model),
        summary_model=raw.get("summary_model", defaults.summary_model),
        max_cost_per_run=raw.get("max_cost_per_run", defaults.max_cost_per_run),
        min_confidence=raw.get("min_confidence", defaults.min_confidence),
        max_inline_comments=raw.get("max_inline_comments", defaults.max_inline_comments),
        context_token_budget=raw.get("context_token_budget", defaults.context_token_budget),
        max_diff_tokens_per_call=raw.get("max_diff_tokens_per_call", defaults.max_diff_tokens_per_call),
        summary=raw.get("summary", defaults.summary),
        dimensions=raw.get("dimensions", list(_DEFAULT_DIMENSIONS)),
        model_params=_validate_params(raw.get("model_params"), "model_params", str(path)),
        verify_model_params=(
            _validate_params(raw["verify_model_params"], "verify_model_params", str(path))
            if "verify_model_params" in raw else None
        ),
        summary_model_params=(
            _validate_params(raw["summary_model_params"], "summary_model_params", str(path))
            if "summary_model_params" in raw else None
        ),
    )
    return endpoints_from_env(config)
