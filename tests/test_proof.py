"""Findings the parser proves. Most of these tests are about silence: the
module is only worth having if it declines to speak whenever it can't tie
a call to the function it names.
"""

import difflib
from pathlib import Path

import pytest

from groundtruth_review.context_engine import build_context, parse_diff
from groundtruth_review.proof import CallShape, Param, _bind, prove_signature_breaks
from groundtruth_review.quality_gate import DropStage, Finding, Severity, run_gate

pytest.importorskip("tree_sitter_language_pack")


def _diff(path: str, base: str, head: str) -> str:
    body = "".join(
        difflib.unified_diff(
            base.splitlines(keepends=True),
            head.splitlines(keepends=True),
            f"a/{path}",
            f"b/{path}",
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}"


def prove(tmp_path: Path, changed: str, base: str, head: str, **other_files: str):
    """Write the head tree, build the context the real pipeline would, and
    return whatever the proof module says about the change to `changed`."""
    (tmp_path / changed).write_text(head)
    for name, text in other_files.items():
        (tmp_path / name.replace("__", "/")).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name.replace("__", "/")).write_text(text)
    diffmap = parse_diff(_diff(changed, base, head))
    ctx = build_context(tmp_path, diffmap, {changed: head}, {changed: base})
    return prove_signature_breaks(tmp_path, ctx.signature_changes, diffmap, {changed: head}, {changed: base})


OLD = "def calculate_discount(price):\n    return price * 0.9\n"
NEW = "def calculate_discount(price, promos):\n    return price * (1 - sum(promos))\n"
CHECKOUT = (
    "from invoice import calculate_discount\n\n\n"
    "def checkout(price):\n    return calculate_discount(price)\n"
)


# ------------------------------------------------------------------ binding


def P(name, kind="pos", default=False):
    return Param(name, kind, default)


def test_a_call_missing_a_required_argument_is_named():
    reason = _bind("f", [P("a"), P("b")], CallShape(1, ()))
    assert reason == "does not pass required argument 'b'"


def test_several_missing_arguments_are_all_listed():
    assert _bind("f", [P("a"), P("b"), P("c")], CallShape(1, ())) == (
        "does not pass required arguments 'b', 'c'"
    )


def test_a_default_makes_the_argument_optional():
    assert _bind("f", [P("a"), P("b", default=True)], CallShape(1, ())) is None


def test_too_many_positional_arguments():
    assert _bind("f", [P("a")], CallShape(3, ())) == "passes 3 positional arguments, but f takes 1"


def test_too_many_says_at_most_when_there_are_defaults():
    reason = _bind("f", [P("a"), P("b", default=True)], CallShape(3, ()))
    assert reason == "passes 3 positional arguments, but f takes at most 2"


def test_varargs_absorb_extra_positionals():
    assert _bind("f", [P("a"), P("rest", "varargs")], CallShape(5, ())) is None


def test_an_unknown_keyword_is_rejected_unless_there_is_varkw():
    assert _bind("f", [P("a")], CallShape(1, ("zz",))) == "passes unknown keyword 'zz'"
    assert _bind("f", [P("a"), P("kw", "varkw")], CallShape(1, ("zz",))) is None


def test_a_value_given_both_positionally_and_by_keyword_is_rejected():
    assert _bind("f", [P("a")], CallShape(1, ("a",))) == "passes 'a' twice, positionally and by keyword"


def test_keyword_only_parameters_can_only_be_given_by_keyword():
    params = [P("a"), P("flag", "kwonly")]
    assert _bind("f", params, CallShape(1, ())) == "does not pass required argument 'flag'"
    assert _bind("f", params, CallShape(1, ("flag",))) is None


def test_positional_only_parameters_cannot_be_given_by_keyword():
    params = [P("a", "posonly")]
    assert _bind("f", params, CallShape(0, ("a",))) == "passes unknown keyword 'a'"


# --------------------------------------------------------- what it proves


def test_a_caller_that_still_uses_the_old_signature_is_proven_broken(tmp_path):
    (finding,) = prove(tmp_path, "invoice.py", OLD, NEW, **{"checkout.py": CHECKOUT})
    assert finding.file == "invoice.py"
    assert finding.line == 1  # the changed signature, where the diff can place it
    assert finding.quoted_code == "def calculate_discount(price, promos):"
    assert finding.severity is Severity.HIGH
    assert "checkout.py:5" in finding.title and "'promos'" in finding.title
    assert finding.proof.startswith("Checked by parsing, not by a model:")


def test_a_compatible_change_proves_nothing(tmp_path):
    head = "def calculate_discount(price, promos=None):\n    return price\n"
    assert prove(tmp_path, "invoice.py", OLD, head, **{"checkout.py": CHECKOUT}) == []


def test_a_call_that_was_already_broken_is_not_blamed_on_this_change(tmp_path):
    broken_before = CHECKOUT.replace("calculate_discount(price)", "calculate_discount(price, 1, 2, 3)")
    # four positional arguments fit neither the old signature nor the new one
    assert prove(tmp_path, "invoice.py", OLD, NEW, **{"checkout.py": broken_before}) == []


def test_two_functions_with_one_name_are_not_proven(tmp_path):
    other = "def calculate_discount(a, b, c):\n    return a\n"
    assert prove(tmp_path, "invoice.py", OLD, NEW, **{"checkout.py": CHECKOUT, "legacy.py": other}) == []


def test_a_same_named_function_imported_from_elsewhere_is_not_ours(tmp_path):
    theirs = CHECKOUT.replace("from invoice import", "from somewhere_else import")
    assert prove(tmp_path, "invoice.py", OLD, NEW, **{"checkout.py": theirs}) == []


def test_a_bare_call_with_no_import_at_all_is_not_proven(tmp_path):
    unimported = "def checkout(price):\n    return calculate_discount(price)\n"
    assert prove(tmp_path, "invoice.py", OLD, NEW, **{"checkout.py": unimported}) == []


def test_a_call_using_star_arguments_is_skipped_because_its_shape_is_unknown(tmp_path):
    # Built so that reading `*args` as any fixed number of arguments would
    # flag it: the old signature accepts a bare call, the new one wants `promos`.
    base = "def calculate_discount(price=0):\n    return price\n"
    head = "def calculate_discount(price=0, *, promos):\n    return price\n"
    starred = CHECKOUT.replace("calculate_discount(price)", "calculate_discount(*args)")
    assert prove(tmp_path, "invoice.py", base, head, **{"checkout.py": starred}) == []


def test_a_recursive_call_that_was_updated_along_with_the_signature_is_fine(tmp_path):
    base = "def walk(node):\n    return walk(node.next)\n"
    head = "def walk(node, depth):\n    return walk(node.next, depth + 1)\n"
    assert prove(tmp_path, "tree.py", base, head) == []


def test_a_recursive_call_the_author_forgot_to_update_is_a_real_break(tmp_path):
    # the function is its own caller here, and it is still passing the old arguments
    base = "def walk(node):\n    return walk(node.next)\n"
    head = "def walk(node, depth):\n    return walk(node.next)\n"
    (finding,) = prove(tmp_path, "tree.py", base, head)
    assert "tree.py:2" in finding.title and "'depth'" in finding.title


def test_a_method_is_only_checked_against_self_calls_inside_its_own_class(tmp_path):
    base = (
        "class Cart:\n    def add(self, item):\n        return item\n\n"
        "    def add_all(self, items):\n        return [self.add(i) for i in items]\n"
    )
    head = base.replace("def add(self, item):", "def add(self, item, qty):")
    (finding,) = prove(tmp_path, "cart.py", base, head)
    assert "cart.py:6" in finding.title and "'qty'" in finding.title


def test_a_method_called_on_some_other_object_is_not_assumed_to_be_ours(tmp_path):
    # Same file, same class: only the receiver says this `add` is another object's.
    base = (
        "class Cart:\n    def add(self, item):\n        return item\n\n"
        "    def merge(self, other):\n        return other.add(1)\n"
    )
    head = base.replace("def add(self, item):", "def add(self, item, qty):")
    assert prove(tmp_path, "cart.py", base, head) == []


def test_a_subclass_calling_the_changed_method_through_self_is_checked(tmp_path):
    # `self.add` in a subclass can only be reaching the one `add` the repo defines
    base = (
        "class Cart:\n    def add(self, item):\n        return item\n\n\n"
        "class Premium(Cart):\n    def bulk(self, items):\n        return [self.add(i) for i in items]\n"
    )
    head = base.replace("def add(self, item):", "def add(self, item, qty):")
    (finding,) = prove(tmp_path, "cart.py", base, head)
    assert "cart.py:8" in finding.title


def test_a_method_called_from_another_file_is_not_checked(tmp_path):
    # A class with the same name in another file, so only the file says it isn't ours.
    base = "class Cart:\n    def add(self, item):\n        return item\n"
    head = base.replace("def add(self, item):", "def add(self, item, qty):")
    lookalike = "class Cart:\n    def other(self):\n        return self.add(1)\n"
    assert prove(tmp_path, "cart.py", base, head, **{"lookalike.py": lookalike}) == []


def test_a_decorated_function_is_left_alone(tmp_path):
    base = (
        "import functools\n\n\n@functools.cache\ndef fetch(a):\n    return a\n\n\n"
        "def use():\n    return fetch(1)\n"
    )
    head = base.replace("def fetch(a):", "def fetch(a, b):")
    assert prove(tmp_path, "m.py", base, head) == []


def test_files_that_are_not_python_are_ignored(tmp_path):
    base = "function f(a) { return a }\nf(1)\n"
    head = "function f(a, b) { return a }\nf(1)\n"
    assert prove(tmp_path, "m.js", base, head) == []


def test_several_broken_calls_become_one_finding_that_counts_them(tmp_path):
    many = (
        "from invoice import calculate_discount\n\n\n"
        + "".join(f"def c{i}(p):\n    return calculate_discount(p)\n\n\n" for i in range(5))
    )
    (finding,) = prove(tmp_path, "invoice.py", OLD, NEW, **{"checkout.py": many})
    assert finding.title.endswith("(and 4 more call sites)")
    assert finding.proof.count("checkout.py:") == 3 and finding.proof.endswith("and 2 more")


def test_the_finding_anchors_on_the_changed_line_of_a_multiline_signature(tmp_path):
    base = "def fetch(\n    a,\n):\n    return a\n"
    head = "def fetch(\n    a,\n    b,\n):\n    return a\n"
    caller = "from m import fetch\n\n\ndef use():\n    return fetch(1)\n"
    (finding,) = prove(tmp_path, "m.py", base, head, **{"use.py": caller})
    assert finding.line == 3 and finding.quoted_code == "b,"


# ------------------------------------------------------------ in the gate


class _NeverAsked:
    """A verifier that fails the test if the gate consults it."""

    def complete_json(self, system, user):
        raise AssertionError("a proven finding must not be sent to the skeptic")


def _proven(**overrides) -> Finding:
    fields = dict(
        file="invoice.py", line=1, category="correctness", severity=Severity.HIGH, confidence=0.95,
        title="t", quoted_code="def calculate_discount(price, promos):", proof="Checked by parsing",
    )
    fields.update(overrides)
    return Finding(**fields)


def test_a_proven_finding_is_posted_without_asking_a_second_model():
    report = run_gate(
        [_proven()], [NEW], lambda f: NEW, _NeverAsked(), min_confidence=0.7
    )
    assert [v.finding.title for v in report.posted] == ["t"]
    assert report.posted[0].combined_confidence == pytest.approx(0.95)


def test_a_proven_finding_still_has_to_quote_real_code():
    report = run_gate([_proven(quoted_code="def nope():")], [NEW], lambda f: NEW, _NeverAsked())
    assert report.posted == [] and report.dropped[0].dropped_at is DropStage.HALLUCINATION


def test_a_proven_finding_is_not_posted_twice(tmp_path):
    first = run_gate([_proven()], [NEW], lambda f: NEW, _NeverAsked())
    seen = {first.posted[0].fingerprint}
    again = run_gate([_proven()], [NEW], lambda f: NEW, _NeverAsked(), seen_fingerprints=seen)
    assert again.posted == [] and again.dropped[0].dropped_at is DropStage.DEDUPE
