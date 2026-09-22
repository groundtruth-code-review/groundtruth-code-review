from groundtruth.context_engine.diffmap import parse_diff
from groundtruth.context_engine.engine import build_context

HEAD_INVOICE = (
    "class Billing:\n"
    "    def calculate_discount(self, price, promos):\n"
    "        rate = sum(p.rate for p in promos)\n"
    "        return price * rate\n"
)

BASE_INVOICE = (
    "class Billing:\n"
    "    def calculate_discount(self, price):\n"
    "        return price * 0.9\n"
)

CHECKOUT = (
    "from invoice import Billing\n\n"
    "def checkout(price):\n"
    "    b = Billing()\n"
    "    return b.calculate_discount(price)\n"
)

DIFF = (
    "diff --git a/invoice.py b/invoice.py\n"
    "index 0000000..1111111 100644\n"
    "--- a/invoice.py\n"
    "+++ b/invoice.py\n"
    "@@ -1,3 +1,4 @@\n"
    " class Billing:\n"
    "-    def calculate_discount(self, price):\n"
    "-        return price * 0.9\n"
    "+    def calculate_discount(self, price, promos):\n"
    "+        rate = sum(p.rate for p in promos)\n"
    "+        return price * rate\n"
)


def _write_repo(tmp_path):
    (tmp_path / "invoice.py").write_text(HEAD_INVOICE)
    (tmp_path / "checkout.py").write_text(CHECKOUT)
    return tmp_path


def test_includes_the_changed_function_itself(tmp_path):
    repo = _write_repo(tmp_path)
    diffmap = parse_diff(DIFF)
    ctx = build_context(
        repo,
        diffmap,
        head_sources={"invoice.py": HEAD_INVOICE},
        base_sources={"invoice.py": BASE_INVOICE},
    )
    changed = [b for b in ctx.blocks if b.tier == 0]
    assert len(changed) == 1
    assert "calculate_discount" in changed[0].text
    assert changed[0].must_include is True


def test_detects_signature_change_and_promotes_caller(tmp_path):
    repo = _write_repo(tmp_path)
    diffmap = parse_diff(DIFF)
    ctx = build_context(
        repo,
        diffmap,
        head_sources={"invoice.py": HEAD_INVOICE},
        base_sources={"invoice.py": BASE_INVOICE},
    )

    changed_block = next(b for b in ctx.blocks if b.tier == 0)
    assert "signature changed" in changed_block.label

    caller_blocks = [b for b in ctx.blocks if b.tier == 1]
    assert len(caller_blocks) == 1
    assert "checkout.py" in caller_blocks[0].label
    assert caller_blocks[0].must_include is True  # promoted, not just nice-to-have


def test_without_base_source_no_signature_promotion(tmp_path):
    repo = _write_repo(tmp_path)
    diffmap = parse_diff(DIFF)
    ctx = build_context(repo, diffmap, head_sources={"invoice.py": HEAD_INVOICE})

    changed_block = next(b for b in ctx.blocks if b.tier == 0)
    assert "signature changed" not in changed_block.label


def test_must_include_blocks_survive_a_tiny_budget(tmp_path):
    repo = _write_repo(tmp_path)
    diffmap = parse_diff(DIFF)
    ctx = build_context(
        repo,
        diffmap,
        head_sources={"invoice.py": HEAD_INVOICE},
        base_sources={"invoice.py": BASE_INVOICE},
        budget_tokens=1,  # absurdly small on purpose
    )
    # must_include blocks are never dropped, no matter how small the budget
    assert any(b.tier == 0 for b in ctx.blocks)


def test_missing_file_in_head_sources_is_skipped_not_crashed(tmp_path):
    repo = _write_repo(tmp_path)
    diffmap = parse_diff(DIFF)
    ctx = build_context(repo, diffmap, head_sources={})
    assert ctx.blocks == []


# --- one function, one block ---------------------------------------------

_BOTH_HEAD = (
    "def calculate_discount(price, promos):\n"
    "    rate = 1 - sum(p.rate for p in promos)\n"
    "    return price * rate\n"
    "\n"
    "\n"
    "def invoice_total(price, promos):\n"
    "    return calculate_discount(price, promos)\n"
)

_BOTH_BASE = (
    "def calculate_discount(price):\n"
    "    return price * 0.9\n"
    "\n"
    "\n"
    "def invoice_total(price):\n"
    "    return calculate_discount(price)\n"
)

_BOTH_DIFF = (
    "diff --git a/invoice.py b/invoice.py\n"
    "--- a/invoice.py\n"
    "+++ b/invoice.py\n"
    "@@ -1,6 +1,7 @@\n"
    "-def calculate_discount(price):\n"
    "-    return price * 0.9\n"
    "+def calculate_discount(price, promos):\n"
    "+    rate = 1 - sum(p.rate for p in promos)\n"
    "+    return price * rate\n"
    " \n"
    " \n"
    "-def invoice_total(price):\n"
    "-    return calculate_discount(price)\n"
    "+def invoice_total(price, promos):\n"
    "+    return calculate_discount(price, promos)\n"
)


def test_a_changed_function_that_calls_the_changed_symbol_is_not_included_twice(tmp_path):
    # invoice_total is both changed code and a caller of calculate_discount.
    # Including it in both roles sent the same function to the model twice.
    (tmp_path / "invoice.py").write_text(_BOTH_HEAD)

    ctx = build_context(
        repo_root=tmp_path,
        diffmap=parse_diff(_BOTH_DIFF),
        head_sources={"invoice.py": _BOTH_HEAD},
        base_sources={"invoice.py": _BOTH_BASE},
    )

    spans = [block.label.split(" ")[0] for block in ctx.blocks]
    assert len(spans) == len(set(spans)), f"a span appears more than once: {spans}"
    assert "invoice.py#L6-7" in spans
    texts = [block.text for block in ctx.blocks]
    assert len(texts) == len(set(texts))


def test_a_caller_in_an_untouched_file_is_still_included(tmp_path):
    # the dedupe must not swallow the caller the whole design exists to catch
    (tmp_path / "invoice.py").write_text(_BOTH_HEAD)
    (tmp_path / "checkout.py").write_text(
        "from invoice import calculate_discount\n\n"
        "def checkout(price):\n"
        "    return calculate_discount(price)\n"
    )

    ctx = build_context(
        repo_root=tmp_path,
        diffmap=parse_diff(_BOTH_DIFF),
        head_sources={"invoice.py": _BOTH_HEAD},
        base_sources={"invoice.py": _BOTH_BASE},
    )

    labels = " ".join(block.label for block in ctx.blocks)
    assert "checkout.py#L3-4 (caller of calculate_discount, signature changed)" in labels
