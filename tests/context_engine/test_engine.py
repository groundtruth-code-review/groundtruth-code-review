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


# ------------------------------------------------- callers further out, and callees

import difflib  # noqa: E402

from groundtruth_review.context_engine import engine as engine_module  # noqa: E402
from groundtruth_review.context_engine.engine import _callee_names  # noqa: E402


def _ctx_for(tmp_path, changed, base, head, budget=25_000, **options):
    """Context for a one-file change; every other keyword is a file in the repo."""
    files = options.pop("files", {})
    (tmp_path / changed).write_text(head)
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    body = "".join(
        difflib.unified_diff(
            base.splitlines(keepends=True), head.splitlines(keepends=True), f"a/{changed}", f"b/{changed}"
        )
    )
    diffmap = parse_diff(f"diff --git a/{changed} b/{changed}\n{body}")
    return build_context(
        tmp_path, diffmap, {changed: head}, {changed: base}, budget_tokens=budget, **options
    )


def _labels(ctx):
    return [b.label for b in ctx.blocks]


LEAF_BASE = "def calculate_discount(price):\n    return price * 0.9\n"
LEAF_HEAD = "def calculate_discount(price):\n    return price * 0.8\n"
CHAIN = {
    "checkout.py": (
        "from leaf import calculate_discount\n\n\ndef checkout(p):\n    return calculate_discount(p)\n"
    ),
    "order.py": "from checkout import checkout\n\n\ndef place_order():\n    return checkout(5)\n",
}


def test_callers_of_callers_are_included_and_say_how_far_out_they_are(tmp_path):
    ctx = _ctx_for(tmp_path, "leaf.py", LEAF_BASE, LEAF_HEAD, files=CHAIN)
    (far,) = [b for b in ctx.blocks if "2 hops" in b.label]
    assert "order.py" in far.label and "caller of checkout, 2 hops from calculate_discount" in far.label
    assert far.tier == 3 and far.must_include is False


def test_depth_one_stops_at_the_direct_callers(tmp_path):
    ctx = _ctx_for(tmp_path, "leaf.py", LEAF_BASE, LEAF_HEAD, files=CHAIN, caller_depth=1)
    assert not any("hops" in label for label in _labels(ctx))
    assert any("caller of calculate_discount" in label for label in _labels(ctx))


def test_two_functions_calling_each_other_do_not_loop_or_repeat(tmp_path):
    base = "def ping(n):\n    return pong(n)\n\n\ndef pong(n):\n    return ping(n)\n"
    head = base.replace("return pong(n)", "return pong(n - 1)")
    ctx = _ctx_for(tmp_path, "pp.py", base, head, caller_depth=3)
    spans = [b.label.split(" ")[0] for b in ctx.blocks]
    assert len(spans) == len(set(spans))  # no function sent twice


def test_a_caller_two_steps_out_that_is_also_a_direct_caller_is_sent_once(tmp_path):
    both = {
        "checkout.py": CHAIN["checkout.py"],
        "order.py": "from checkout import checkout\nfrom leaf import calculate_discount\n\n\n"
                    "def place_order():\n    calculate_discount(1)\n    return checkout(5)\n",
    }
    ctx = _ctx_for(tmp_path, "leaf.py", LEAF_BASE, LEAF_HEAD, files=both)
    order_blocks = [b for b in ctx.blocks if b.label.startswith("order.py")]
    assert len(order_blocks) == 1 and order_blocks[0].tier == 1  # kept as the nearer, higher-priority one


def test_searches_past_the_first_step_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "_MAX_INDIRECT_SEARCHES", 0)
    ctx = _ctx_for(tmp_path, "leaf.py", LEAF_BASE, LEAF_HEAD, files=CHAIN)
    assert not any("hops" in label for label in _labels(ctx))


def test_when_only_the_nearer_blocks_fit_the_far_caller_is_what_gets_cut(tmp_path):
    everything = _ctx_for(tmp_path, "leaf.py", LEAF_BASE, LEAF_HEAD, files=CHAIN)
    near = sum(b.tokens for b in everything.blocks if b.tier <= 1)

    ctx = _ctx_for(tmp_path, "leaf.py", LEAF_BASE, LEAF_HEAD, files=CHAIN, budget=near)
    assert any(label.startswith("checkout.py") for label in _labels(ctx))
    assert not any("checkout.py" in cut for cut in ctx.cut)  # the nearer caller is intact
    assert any("order.py" in cut for cut in ctx.cut)  # the one two steps out is not


CALLEE_FILES = {"rates.py": "def parse_rate(text):\n    return float(text) if text else None\n"}
CALLEE_BASE = "def apply(price, row):\n    return price\n"
CALLEE_HEAD = "def apply(price, row):\n    rate = parse_rate(row['rate'])\n    return price * rate\n"


def test_a_function_the_new_code_calls_is_included_so_its_behaviour_is_visible(tmp_path):
    ctx = _ctx_for(tmp_path, "pricing.py", CALLEE_BASE, CALLEE_HEAD, files=CALLEE_FILES)
    (callee,) = [b for b in ctx.blocks if "callee of apply" in b.label]
    assert "rates.py" in callee.label and "return float(text) if text else None" in callee.text
    assert callee.tier == 2 and callee.must_include is False


def test_a_call_that_was_already_there_is_not_looked_up(tmp_path):
    base = "def apply(price, row):\n    rate = parse_rate(row['rate'])\n    return price * rate\n"
    head = base.replace("return price * rate", "return price * rate * 2")
    ctx = _ctx_for(tmp_path, "pricing.py", base, head, files=CALLEE_FILES)
    assert not any("callee of" in label for label in _labels(ctx))


def test_two_definitions_of_the_called_name_means_no_callee_block(tmp_path):
    twice = {**CALLEE_FILES, "old_rates.py": "def parse_rate(a, b):\n    return a\n"}
    ctx = _ctx_for(tmp_path, "pricing.py", CALLEE_BASE, CALLEE_HEAD, files=twice)
    assert not any("callee of" in label for label in _labels(ctx))


def test_callees_can_be_turned_off(tmp_path):
    ctx = _ctx_for(
        tmp_path, "pricing.py", CALLEE_BASE, CALLEE_HEAD, files=CALLEE_FILES, include_callees=False
    )
    assert not any("callee of" in label for label in _labels(ctx))


def test_a_callee_that_is_itself_changed_in_the_pull_request_is_sent_once(tmp_path):
    head = "def apply(price, row):\n    rate = helper(row)\n    return price * rate\n\n\n" \
           "def helper(row):\n    return 2\n"
    base = "def apply(price, row):\n    return price\n\n\ndef helper(row):\n    return 1\n"
    ctx = _ctx_for(tmp_path, "pricing.py", base, head)
    helper_blocks = [b for b in ctx.blocks if "#L6-7" in b.label or "helper" in b.text]
    assert len([b for b in helper_blocks if b.text.startswith("def helper")]) == 1


def test_callee_names_come_only_from_calls_in_the_code_that_matter():
    lines = [
        "# parse_rate(this) is a comment",
        "    rate = parse_rate(row)",
        "    total = obj.compute(rate)",          # a method on something else: not looked up
        "    return self.finish(total)",           # a call into this class: looked up
        "    if len(rows): pass",                  # keyword and builtin
        "def inner(x):",                           # a definition, not a call
        "    go(1)",                               # too short to be a useful name
        "    apply(1)",                            # the function's own name
    ]
    assert _callee_names(lines, own_name="apply") == ["parse_rate", "finish"]


def test_definition_searches_are_capped_per_review(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "_MAX_DEFINITION_SEARCHES", 0)
    ctx = _ctx_for(tmp_path, "pricing.py", CALLEE_BASE, CALLEE_HEAD, files=CALLEE_FILES)
    assert not any("callee of" in label for label in _labels(ctx))


def test_a_name_is_only_searched_for_once_per_review(tmp_path, monkeypatch):
    searched = []
    real = engine_module.find_definitions

    def counting(root, name):
        searched.append(name)
        return real(root, name)

    monkeypatch.setattr(engine_module, "find_definitions", counting)
    base = "def one(row):\n    return row\n\n\ndef two(row):\n    return row\n"
    head = (
        "def one(row):\n    return parse_rate(row)\n\n\ndef two(row):\n    return parse_rate(row)\n"
    )
    _ctx_for(tmp_path, "pricing.py", base, head, files=CALLEE_FILES)
    assert searched.count("parse_rate") == 1
