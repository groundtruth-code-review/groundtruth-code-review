"""A thin, embedded wrapper around LiteLLM — this is the whole "any model,
own keys" story.

Deliberately not a standalone proxy service: LiteLLM is used here as a
Python dependency, so picking a model is a one-line config change and the
API key is whatever environment variable the chosen provider expects
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...). Point `model` at
`ollama/<model>` and there's no key and no network cost at all.

For org deployments that want centralized spend caps, budgets, and an audit
log, none of that is reimplemented here — set `api_base` to a self-hosted
LiteLLM *proxy* (a separate, already-open-source piece) and every call routes
through it instead of the provider directly. Same client, same code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import litellm

# Two provider quirks the original design hit in practice and had to work
# around — kept here because they're real, not hypothetical:
#   1. Some reasoning models reject `temperature` outright. LiteLLM's
#      `drop_params` handles this without the caller needing to know which
#      models are picky.
#   2. JSON mode on some providers only reliably triggers when the word
#      "json" appears in a *user* message, not just a system prompt.
_JSON_REMINDER = "\n\nReply with ONLY the JSON object — no prose, no markdown fences."


@dataclass(frozen=True)
class CostEstimate:
    model: str
    prompt_tokens: int
    estimated_completion_tokens: int
    estimated_cost_usd: float | None  # None when the model isn't in LiteLLM's cost registry


class LlmClient:
    def __init__(
        self,
        model: str,
        api_base: str | None = None,
        api_key: str | None = None,
        max_output_tokens: int = 4096,
        params: dict | None = None,
    ):
        """`api_base` left unset calls the provider directly. Set it to a
        self-hosted LiteLLM proxy URL for org mode (spend caps, audit log,
        per-team budgets — all live in that proxy, not here).

        `max_output_tokens` is set explicitly rather than left to each
        provider's own default: a response that gets cut off mid-JSON fails
        to parse, and `complete_json` raises on that — silently, from the
        caller's point of view, that reads as "the model found nothing,"
        which is a worse failure than a clearly-too-small ceiling. 4096 is
        generous for one file's worth of findings (the normal case now that
        review calls are batched per file, not per whole diff).

        `params` carries a stage's sampling settings from config --
        temperature, top_p, max_tokens -- already validated there. Reasoning
        models are the usual reason to set them: many expect temperature 1,
        and they spend part of max_tokens thinking before they answer, so a
        budget sized for a plain model can run out mid-JSON.
        """
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.params = dict(params or {})

    def complete_json(self, system: str, user: str, timeout: float = 120.0) -> dict:
        """One call, JSON in, JSON out. Raises on a genuinely broken response
        (bad JSON, empty content) — the caller (the quality gate) decides
        what a failed LLM call means for the finding under review, this
        layer's only job is talking to the model faithfully.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user + _JSON_REMINDER},
        ]
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            # Low by default: the same diff should get the same review.
            "temperature": self.params.get("temperature", 0.1),
            "timeout": timeout,
            "max_tokens": self.params.get("max_tokens", self.max_output_tokens),
            # Never streamed. The reply is parsed as one JSON object, so it
            # has to arrive whole; config rejects stream for the same reason.
            "stream": False,
            "drop_params": True,  # silently drop params a given provider can't take
        }
        if "top_p" in self.params:
            kwargs["top_p"] = self.params["top_p"]
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key

        response = litellm.completion(**kwargs)
        content = response.choices[0].message.content or ""
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:]
        return json.loads(content)

    def estimate_cost(self, system: str, user: str, expected_completion_tokens: int = 800) -> CostEstimate:
        """A *before-the-call* estimate, for the personal-use safety ceiling
        (`max_cost_per_run` / `--dry-run`). `expected_completion_tokens` is a
        rough default — real usage varies — this is a guardrail, not a bill.
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            prompt_tokens = litellm.token_counter(model=self.model, messages=messages)
        except Exception:
            prompt_tokens = max(1, len(system + user) // 4)

        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=expected_completion_tokens,
            )
            estimated_cost = round(prompt_cost + completion_cost, 6)
        except Exception:
            # unknown model in the registry (e.g. a local Ollama model) — free or untracked
            estimated_cost = None

        return CostEstimate(
            model=self.model,
            prompt_tokens=prompt_tokens,
            estimated_completion_tokens=expected_completion_tokens,
            estimated_cost_usd=estimated_cost,
        )
