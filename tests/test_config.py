import pytest

from groundtruth.config import (
    ENV_BASE_URL,
    ENV_SUMMARY_BASE_URL,
    ENV_VERIFY_BASE_URL,
    Config,
    ConfigError,
    load_config,
    normalize_base_url,
)


def test_missing_config_file_returns_defaults(tmp_path):
    config = load_config(tmp_path / "does_not_exist.yml")
    assert config == Config()


def test_none_path_returns_defaults():
    assert load_config(None) == Config()


def test_loads_real_values(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text(
        "model: openai/gpt-4o\n"
        "max_cost_per_run: 0.50\n"
        "min_confidence: 0.8\n"
        "max_inline_comments: 5\n"
        "dimensions: [correctness, security]\n"
    )
    config = load_config(path)
    assert config.model == "openai/gpt-4o"
    assert config.max_cost_per_run == 0.50
    assert config.min_confidence == 0.8
    assert config.max_inline_comments == 5
    assert config.dimensions == ["correctness", "security"]


def test_partial_config_fills_in_defaults_for_the_rest(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text("model: ollama/qwen2.5-coder\n")
    config = load_config(path)
    assert config.model == "ollama/qwen2.5-coder"
    assert config.min_confidence == Config().min_confidence  # untouched default


def test_empty_file_returns_defaults(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text("")
    assert load_config(path) == Config()


def test_rejects_a_config_holding_something_key_shaped(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text('model: anthropic/claude-sonnet-5\napi_key: "sk-ant-abcdefghijklmnopqrstuvwxyz"\n')
    with pytest.raises(ConfigError, match="looks like it contains a live API key"):
        load_config(path)


def test_rejects_a_long_opaque_value_even_without_a_known_prefix(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    # 32-char opaque value, no known key prefix
    path.write_text(
        "model: anthropic/claude-sonnet-5\nsome_field: aB3dE7fH9jK1mN5pQ8rT0vX2yZ4bC6dX\n"
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_mapping_yaml_is_a_config_error(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_tier_models_default_to_the_one_configured_model(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text("model: anthropic/claude-sonnet-5\n")
    config = load_config(path)
    # an unset tier must not silently switch the user to a different model
    assert config.review_model == "anthropic/claude-sonnet-5"
    assert config.resolved_verify_model == "anthropic/claude-sonnet-5"
    assert config.resolved_summary_model == "anthropic/claude-sonnet-5"


def test_tier_models_can_be_split(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text(
        "model: anthropic/claude-opus-5\n"
        "verify_model: anthropic/claude-haiku-4-5-20251001\n"
    )
    config = load_config(path)
    assert config.review_model == "anthropic/claude-opus-5"
    assert config.resolved_verify_model == "anthropic/claude-haiku-4-5-20251001"
    # the summary follows the cheap tier when it has no setting of its own
    assert config.resolved_summary_model == "anthropic/claude-haiku-4-5-20251001"


def test_summary_and_diff_budget_are_configurable(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text("summary: false\nmax_diff_tokens_per_call: 1200\n")
    config = load_config(path)
    assert config.summary is False
    assert config.max_diff_tokens_per_call == 1200


# --- verification can run on a different provider from review ------------

def test_the_verifier_can_be_a_different_provider(tmp_path):
    # the point of the skeptic pass is an independent second opinion; a
    # different provider's model does not share the first model's blind spots
    path = tmp_path / ".groundtruth.yml"
    path.write_text(
        "model: anthropic/claude-sonnet-5\n"
        "verify_model: openai/gpt-4o-mini\n"
    )
    config = load_config(path)
    assert config.review_model.startswith("anthropic/")
    assert config.resolved_verify_model.startswith("openai/")


def test_it_works_the_other_way_round_too(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text(
        "model: openai/gpt-4o\n"
        "verify_model: anthropic/claude-haiku-4-5-20251001\n"
    )
    config = load_config(path)
    assert config.review_model.startswith("openai/")
    assert config.resolved_verify_model.startswith("anthropic/")


def test_three_providers_can_share_one_run(tmp_path):
    path = tmp_path / ".groundtruth.yml"
    path.write_text(
        "model: openai/gpt-4o\n"
        "verify_model: anthropic/claude-haiku-4-5-20251001\n"
        "summary_model: ollama/qwen2.5-coder\n"
    )
    config = load_config(path)
    providers = {m.split("/")[0] for m in (
        config.review_model, config.resolved_verify_model, config.resolved_summary_model
    )}
    assert providers == {"openai", "anthropic", "ollama"}


# --- endpoints never come from the reviewed repository -------------------


@pytest.mark.parametrize(
    "key", ["llm_base_url", "base_url", "api_base", "verify_base_url", "summary_base_url"]
)
def test_an_endpoint_in_the_config_file_is_refused(tmp_path, key):
    # the pull request under review can edit this file; an endpoint here
    # would let it choose which server receives your API key
    path = tmp_path / ".groundtruth.yml"
    path.write_text(f"model: openai/gpt-4o\n{key}: https://attacker.example/v1\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "pull request" in str(exc.value)
    assert ENV_BASE_URL in str(exc.value)


def test_endpoints_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://integrate.api.nvidia.com/v1")
    config = load_config(tmp_path / "missing.yml")
    assert config.review_base_url == "https://integrate.api.nvidia.com/v1"


def test_the_environment_applies_even_with_no_config_file(monkeypatch):
    # a repository that never added .groundtruth.yml still gets the endpoint
    # its CI sets
    monkeypatch.setenv(ENV_VERIFY_BASE_URL, "https://integrate.api.nvidia.com/v1")
    assert load_config(None).resolved_verify_base_url == "https://integrate.api.nvidia.com/v1"


@pytest.mark.parametrize("raw, expected", [
    ("https://integrate.api.nvidia.com/v1/", "https://integrate.api.nvidia.com/v1"),
    ("  https://litellm.internal/  ", "https://litellm.internal"),
    ("", None),
    ("   ", None),
    (None, None),
])
def test_a_trailing_slash_is_trimmed(raw, expected):
    # LiteLLM routes NVIDIA's endpoint to its own provider and key only on an
    # exact match; "/v1/" silently falls back to OpenAI and the wrong key
    assert normalize_base_url(raw) == expected


# --- each endpoint follows the model it serves ----------------------------

def test_one_endpoint_serves_every_stage_by_default():
    c = Config(llm_base_url="https://proxy.internal")
    assert c.review_base_url == c.resolved_verify_base_url == c.resolved_summary_base_url == "https://proxy.internal"


def test_review_and_verify_can_live_at_different_endpoints():
    c = Config(model="anthropic/claude-sonnet-5", verify_model="nvidia_nim/moonshotai/kimi-k3",
               verify_base_url="https://integrate.api.nvidia.com/v1")
    assert c.review_base_url is None                      # Anthropic's own default
    assert c.resolved_verify_base_url == "https://integrate.api.nvidia.com/v1"


def test_the_summary_follows_the_verifiers_endpoint_when_it_follows_its_model():
    # summary_model is unset, so the summary runs the verifier's model -- and
    # must go to the verifier's endpoint, not send that model to Anthropic
    c = Config(model="anthropic/claude-sonnet-5", verify_model="nvidia_nim/moonshotai/kimi-k3",
               verify_base_url="https://integrate.api.nvidia.com/v1")
    assert c.resolved_summary_model == c.resolved_verify_model
    assert c.resolved_summary_base_url == "https://integrate.api.nvidia.com/v1"


def test_a_summary_with_its_own_model_does_not_borrow_the_verifiers_endpoint():
    c = Config(model="anthropic/claude-sonnet-5", verify_model="nvidia_nim/moonshotai/kimi-k3",
               verify_base_url="https://integrate.api.nvidia.com/v1",
               summary_model="anthropic/claude-haiku-4-5-20251001")
    assert c.resolved_summary_base_url is None            # back to Anthropic's default


def test_an_explicit_summary_endpoint_always_wins():
    c = Config(llm_base_url="https://a", verify_base_url="https://b", summary_base_url="https://c")
    assert c.resolved_summary_base_url == "https://c"


def test_each_variable_sets_its_own_stage(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://a")
    monkeypatch.setenv(ENV_VERIFY_BASE_URL, "https://b")
    monkeypatch.setenv(ENV_SUMMARY_BASE_URL, "https://c")
    c = load_config(None)
    assert (c.review_base_url, c.resolved_verify_base_url, c.resolved_summary_base_url) == (
        "https://a", "https://b", "https://c")


# --- per-model sampling settings ------------------------------------------

def _cfg(tmp_path, text):
    path = tmp_path / ".groundtruth.yml"
    path.write_text(text)
    return load_config(path)


def test_sampling_settings_are_read_per_stage(tmp_path):
    c = _cfg(tmp_path,
             "model: nvidia_nim/moonshotai/kimi-k3\n"
             "model_params: {temperature: 1, top_p: 1, max_tokens: 16384}\n"
             "verify_model: nvidia_nim/z-ai/glm-5.3-flash\n"
             "verify_model_params: {temperature: 0.1, max_tokens: 2048}\n")
    assert c.review_params == {"temperature": 1.0, "top_p": 1.0, "max_tokens": 16384}
    assert c.resolved_verify_params == {"temperature": 0.1, "max_tokens": 2048}


def test_stream_is_refused_with_the_reason(tmp_path):
    # the reply is parsed as one JSON object, so it has to arrive whole
    with pytest.raises(ConfigError) as exc:
        _cfg(tmp_path, "model_params: {temperature: 1, stream: false}\n")
    assert "stream" in str(exc.value)
    assert "whole" in str(exc.value)


@pytest.mark.parametrize("smuggled", ["api_base", "api_key", "extra_headers", "base_url"])
def test_only_allowlisted_settings_pass(tmp_path, smuggled):
    # the pull request can edit this file; an open mapping would let it route
    # the key somewhere else by calling it a "parameter"
    with pytest.raises(ConfigError) as exc:
        _cfg(tmp_path, f"model_params: {{{smuggled}: https://attacker.example}}\n")
    assert "not a supported setting" in str(exc.value)


@pytest.mark.parametrize("bad", [
    "{temperature: 3}",
    "{top_p: 1.5}",
    "{max_tokens: 0}",
    "{max_tokens: 1000000}",
    "{max_tokens: 12.5}",
    "{temperature: hot}",
    "{temperature: true}",
])
def test_settings_outside_their_bounds_are_refused(tmp_path, bad):
    with pytest.raises(ConfigError):
        _cfg(tmp_path, f"model_params: {bad}\n")


def test_a_non_mapping_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        _cfg(tmp_path, "model_params: [1, 2]\n")


def test_settings_follow_the_model_they_were_tuned_for():
    # verify runs the same model as review, so it gets the same settings --
    # a reasoning model that needs temperature 1 needs it in both stages
    c = Config(model="nvidia_nim/moonshotai/kimi-k3", model_params={"temperature": 1.0})
    assert c.resolved_verify_params == {"temperature": 1.0}
    assert c.resolved_summary_params == {"temperature": 1.0}


def test_settings_do_not_follow_a_different_model():
    # tuned for the reviewer, so they must not be applied to a verifier that
    # names a different model
    c = Config(model="nvidia_nim/moonshotai/kimi-k3", model_params={"temperature": 1.0},
               verify_model="nvidia_nim/z-ai/glm-5.3-flash")
    assert c.resolved_verify_params == {}


def test_the_summary_inherits_the_verifiers_settings_with_its_model():
    c = Config(model="a/x", verify_model="b/y", verify_model_params={"max_tokens": 2048})
    assert c.resolved_summary_params == {"max_tokens": 2048}


def test_no_settings_means_the_defaults():
    assert Config().review_params == {}


# --- NVIDIA ids carry their vendor, so the provider prefix goes in front ---

from groundtruth.config import NVIDIA_CATALOG_URL, check_model_matches_endpoint  # noqa: E402


@pytest.mark.parametrize("model", ["openai/gpt-oss-20b", "moonshotai/kimi-k3", "meta/codellama-70b"])
def test_an_nvidia_id_without_the_provider_prefix_fails_with_the_fix(model):
    # LiteLLM reads the vendor as the provider, so the call would go out as
    # the wrong provider with the wrong key and a shortened model name
    with pytest.raises(ConfigError) as exc:
        check_model_matches_endpoint(model, NVIDIA_CATALOG_URL, "review")
    assert f"nvidia_nim/{model}" in str(exc.value)


def test_a_prefixed_nvidia_id_passes():
    check_model_matches_endpoint("nvidia_nim/openai/gpt-oss-20b", NVIDIA_CATALOG_URL, "review")


def test_other_endpoints_are_not_second_guessed():
    check_model_matches_endpoint("openai/gpt-4o", None, "review")
    check_model_matches_endpoint("openai/my-model", "https://vllm.internal/v1", "review")


def test_every_stage_is_checked():
    c = Config(model="nvidia_nim/moonshotai/kimi-k3", llm_base_url=NVIDIA_CATALOG_URL,
               verify_model="openai/gpt-oss-20b")
    with pytest.raises(ConfigError) as exc:
        c.check_models_match_endpoints()
    assert "verify" in str(exc.value)
