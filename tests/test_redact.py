from groundtruth.redact import describe_error, redact


def test_the_reason_is_kept():
    text = describe_error(RuntimeError("Missing credentials. Set OPENAI_API_KEY"))
    assert text == "RuntimeError: Missing credentials. Set OPENAI_API_KEY"


def test_only_the_first_line_is_kept():
    assert describe_error(ValueError("first line\nsecond line\nthird")) == "ValueError: first line"


def test_long_messages_are_trimmed():
    assert len(describe_error(RuntimeError("x " * 400))) <= 240


def test_an_empty_message_still_names_the_error():
    assert describe_error(TimeoutError()) == "TimeoutError"


# CI logs on a public repository are public, and providers sometimes echo
# part of the key back in their error message
def test_an_openai_style_key_is_redacted():
    out = redact("Incorrect API key provided: sk-proj-abc123def456ghi789")
    assert "sk-proj" not in out and "[redacted]" in out


def test_an_nvidia_key_is_redacted():
    out = redact("401 for key nvapi-AbCdEf1234567890xyz")
    assert "nvapi-" not in out


def test_a_bearer_token_is_redacted():
    out = redact("header was Bearer eyJhbGciOiJIUzI1NiJ9.payload")
    assert "eyJ" not in out


def test_a_long_opaque_token_is_redacted():
    assert "[redacted]" in redact("token=" + "a1B2" * 10)


def test_ordinary_words_survive():
    msg = "Model not found: openai/gpt-oss-20b at integrate.api.nvidia.com"
    assert redact(msg) == msg
