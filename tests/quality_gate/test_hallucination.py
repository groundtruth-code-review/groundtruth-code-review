from groundtruth.quality_gate.hallucination import line_in_changed_hunk, quote_exists


def test_real_quote_is_found():
    evidence = ["def calculate_discount(self, price, promos):\n    return price"]
    assert quote_exists("def calculate_discount(self, price, promos):", evidence) is True


def test_whitespace_differences_are_ignored():
    evidence = ["rate = sum(p.rate for p in promos)"]
    # model re-joined/re-indented the quote — still the same code
    assert quote_exists("rate   =   sum(p.rate  for p in promos)", evidence) is True


def test_fabricated_quote_is_rejected():
    evidence = ["def calculate_discount(self, price, promos):\n    return price"]
    assert quote_exists("rate = sum(p.rate for p in promos or [])", evidence) is False


def test_too_short_quote_is_rejected_even_if_present():
    evidence = ["x = 1\ny = 2\n"]
    assert quote_exists("x = 1", evidence) is False


def test_checks_across_multiple_evidence_blocks():
    evidence = ["block one has nothing relevant", "block two has the_real_target_line(x, y)"]
    assert quote_exists("the_real_target_line(x, y)", evidence) is True


def test_empty_quote_is_rejected():
    assert quote_exists("", ["anything"]) is False


def test_line_inside_a_hunk_is_accepted():
    assert line_in_changed_hunk(12, [(10, 18)]) is True


def test_line_on_a_hunk_boundary_is_accepted():
    assert line_in_changed_hunk(10, [(10, 18)]) is True
    assert line_in_changed_hunk(18, [(10, 18)]) is True


def test_line_outside_every_hunk_is_rejected():
    assert line_in_changed_hunk(9, [(10, 18), (40, 44)]) is False
    assert line_in_changed_hunk(30, [(10, 18), (40, 44)]) is False


def test_a_file_with_no_hunks_rejects_every_line():
    assert line_in_changed_hunk(1, []) is False
