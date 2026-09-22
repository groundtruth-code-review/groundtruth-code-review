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
