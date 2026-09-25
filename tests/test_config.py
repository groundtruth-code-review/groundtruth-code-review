import pytest

from groundtruth.config import Config, ConfigError, load_config


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
