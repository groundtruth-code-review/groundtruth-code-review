from groundtruth.context_engine.callers import find_callers


def _write(root, rel_path, content):
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_finds_call_site_in_another_file(tmp_path):
    _write(
        tmp_path,
        "billing/invoice.py",
        "def calculate_discount(price, promos):\n    return price\n",
    )
    _write(
        tmp_path,
        "checkout/checkout.py",
        "from billing.invoice import calculate_discount\n\n"
        "def checkout(price):\n    return calculate_discount(price, [])\n",
    )

    hits = find_callers(tmp_path, "calculate_discount", from_path="billing/invoice.py")

    assert len(hits) == 1
    assert hits[0].path == "checkout/checkout.py"
    assert hits[0].line == 4


def test_ranks_closer_directory_first(tmp_path):
    _write(tmp_path, "billing/invoice.py", "def helper():\n    pass\n")
    _write(tmp_path, "billing/promos.py", "def x():\n    return helper()\n")
    _write(tmp_path, "far/away/other.py", "def y():\n    return helper()\n")

    hits = find_callers(tmp_path, "helper", from_path="billing/invoice.py")

    assert hits[0].path == "billing/promos.py"


def test_respects_limit(tmp_path):
    _write(tmp_path, "invoice.py", "def helper():\n    pass\n")
    for i in range(10):
        _write(tmp_path, f"caller{i}.py", "def x():\n    return helper()\n")

    hits = find_callers(tmp_path, "helper", from_path="invoice.py", limit=3)

    assert len(hits) == 3


def test_skips_vendor_and_node_modules(tmp_path):
    _write(tmp_path, "invoice.py", "def helper():\n    pass\n")
    _write(tmp_path, "node_modules/pkg/x.py", "def x():\n    return helper()\n")
    _write(tmp_path, "vendor/x.py", "def y():\n    return helper()\n")

    hits = find_callers(tmp_path, "helper", from_path="invoice.py")

    assert hits == []


def test_no_matches_returns_empty_list(tmp_path):
    _write(tmp_path, "invoice.py", "def helper():\n    pass\n")

    assert find_callers(tmp_path, "totally_unused_name", from_path="invoice.py") == []


def test_short_name_is_rejected_to_avoid_noise(tmp_path):
    _write(tmp_path, "invoice.py", "def x():\n    return 1\n")
    assert find_callers(tmp_path, "x", from_path="invoice.py") == []
