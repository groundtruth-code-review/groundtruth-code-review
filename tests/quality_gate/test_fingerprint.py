from groundtruth.quality_gate.fingerprint import fingerprint


def test_same_inputs_produce_same_fingerprint():
    a = fingerprint("invoice.py", "def calculate_discount(price):", "correctness", "return price")
    b = fingerprint("invoice.py", "def calculate_discount(price):", "correctness", "return price")
    assert a == b


def test_survives_rebase_when_content_is_identical():
    # Same file/content/category/quote, framed as "moved to a different
    # location" — the fingerprint has no line numbers in it, so a rebase
    # that shifts everything down ten lines changes nothing here.
    a = fingerprint("invoice.py", "def calculate_discount(price):", "correctness", "return price")
    b = fingerprint("invoice.py", "def calculate_discount(price):", "correctness", "return price")
    assert a == b


def test_editing_the_flagged_code_changes_the_fingerprint():
    before = fingerprint("invoice.py", "def calculate_discount(price):", "correctness", "return price")
    after = fingerprint("invoice.py", "def calculate_discount(price, tax):", "correctness", "return price")
    assert before != after


def test_different_file_changes_the_fingerprint():
    a = fingerprint("invoice.py", "same hunk", "correctness", "same quote")
    b = fingerprint("checkout.py", "same hunk", "correctness", "same quote")
    assert a != b


def test_different_category_changes_the_fingerprint():
    a = fingerprint("invoice.py", "same hunk", "correctness", "same quote")
    b = fingerprint("invoice.py", "same hunk", "security", "same quote")
    assert a != b


def test_two_distinct_findings_in_the_same_hunk_do_not_collapse():
    # DD-019's regression case: two real, different findings whose *hunk*
    # is identical must still get different identities as long as they
    # quote different code — otherwise the second one silently vanishes as
    # a "duplicate" of the first.
    hunk = "def process(a, b):\n    x = a / b\n    y = risky_call(a)\n    return x, y"
    a = fingerprint("proc.py", hunk, "correctness", "x = a / b")
    b = fingerprint("proc.py", hunk, "correctness", "y = risky_call(a)")
    assert a != b


def test_wording_of_the_quote_whitespace_does_not_matter():
    a = fingerprint("invoice.py", "hunk", "correctness", "return price * rate")
    b = fingerprint("invoice.py", "hunk", "correctness", "return   price *   rate")
    assert a == b
