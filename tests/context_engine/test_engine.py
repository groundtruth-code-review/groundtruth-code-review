from groundtruth_review.context_engine.diffmap import parse_diff
from groundtruth_review.context_engine.engine import build_context

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


def _signature_changes(tmp_path, base: str, head: str):
    (tmp_path / "m.py").write_text(head)
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        + "".join(
            f"@@ -1,{len(base.splitlines())} +1,{len(head.splitlines())} @@\n"
            + "".join(f"-{line}\n" for line in base.splitlines())
            + "".join(f"+{line}\n" for line in head.splitlines())
            for _ in [0]
        )
    )
    ctx = build_context(tmp_path, parse_diff(diff), {"m.py": head}, {"m.py": base})
    return ctx.signature_changes


def test_a_parameter_added_to_a_wrapped_signature_counts_as_a_signature_change(tmp_path):
    # the first line is `def f(` both before and after; only a later line changed
    base = "def f(\n    a,\n):\n    return a\n"
    head = "def f(\n    a,\n    b,\n):\n    return a\n"
    changes = _signature_changes(tmp_path, base, head)
    assert [c.name for c in changes] == ["f"]


def test_a_changed_return_annotation_on_one_line_still_counts(tmp_path):
    base = "def f(a) -> int:\n    return 1\n"
    head = "def f(a) -> str:\n    return 'x'\n"
    changes = _signature_changes(tmp_path, base, head)
    assert [c.name for c in changes] == ["f"]


def test_a_change_to_the_body_of_a_wrapped_signature_function_does_not_count(tmp_path):
    base = "def f(\n    a,\n    b,\n):\n    return a\n"
    head = "def f(\n    a,\n    b,\n):\n    return b\n"
    assert _signature_changes(tmp_path, base, head) == []
