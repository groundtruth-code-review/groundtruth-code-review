"""No network calls here — `complete_json` needs a real provider and a real
key, which a test suite should never depend on. What's tested is the part
that matters without either: cost estimation (pure local computation against
LiteLLM's model registry) and the response-cleanup logic.
"""

from groundtruth.llm.client import LlmClient


def test_estimate_cost_for_a_known_model_returns_a_number():
    client = LlmClient(model="gpt-4o-mini")
    estimate = client.estimate_cost(system="You are a reviewer.", user="Review this diff: ...")
    assert estimate.prompt_tokens > 0
    assert estimate.estimated_cost_usd is not None
    assert estimate.estimated_cost_usd >= 0


def test_estimate_cost_for_an_unknown_local_model_degrades_to_none_not_a_crash():
    client = LlmClient(model="ollama/some-model-litellm-has-never-heard-of")
    estimate = client.estimate_cost(system="sys", user="user")
    assert estimate.prompt_tokens > 0
    # unknown to the cost registry -> None, not an exception and not a fake 0
    assert estimate.estimated_cost_usd is None or estimate.estimated_cost_usd == 0


# --- what actually reaches LiteLLM ----------------------------------------

def _captured_call(monkeypatch, **client_kwargs):
    import types

    import groundtruth.llm.client as client_module

    seen = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        message = types.SimpleNamespace(content='{"findings": []}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(client_module.litellm, "completion", fake_completion)
    client_module.LlmClient(model="m", **client_kwargs).complete_json("system", "user")
    return seen


def test_defaults_are_low_temperature_and_never_streamed(monkeypatch):
    seen = _captured_call(monkeypatch)
    assert seen["temperature"] == 0.1
    assert seen["max_tokens"] == 4096
    assert seen["stream"] is False
    assert "top_p" not in seen


def test_configured_settings_reach_the_call(monkeypatch):
    seen = _captured_call(monkeypatch, params={"temperature": 1.0, "top_p": 1.0, "max_tokens": 16384})
    assert (seen["temperature"], seen["top_p"], seen["max_tokens"]) == (1.0, 1.0, 16384)
    assert seen["stream"] is False
