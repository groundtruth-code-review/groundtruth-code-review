import shutil

import pytest

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


# --- the two search paths must agree -------------------------------------

def test_rg_filters_exclude_every_directory_the_python_walk_skips():
    from groundtruth.context_engine.callers import _SKIP_DIRS, rg_filters

    args = rg_filters()
    for directory in _SKIP_DIRS:
        assert f"!**/{directory}/**" in args, f"{directory} is skipped by one path only"


def test_rg_exclusions_come_after_the_extension_globs():
    # ripgrep gives later globs precedence, so excluding before including
    # lets *.py re-admit node_modules/pkg.py
    from groundtruth.context_engine.callers import rg_filters

    args = rg_filters()
    last_include = max(i for i, a in enumerate(args) if a.startswith("*."))
    first_exclude = min(i for i, a in enumerate(args) if a.startswith("!"))
    assert first_exclude > last_include


def test_rg_filters_include_every_searchable_extension_and_cap_size():
    from groundtruth.context_engine.callers import _MAX_FILE_BYTES, _SEARCHABLE_EXT, rg_filters

    args = rg_filters()
    for ext in _SEARCHABLE_EXT:
        assert f"*{ext}" in args
    assert args[args.index("--max-filesize") + 1] == str(_MAX_FILE_BYTES)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed here")
def test_the_rg_path_and_the_python_path_return_the_same_hits(tmp_path):
    # this is the test the container build runs and a bare runner skips:
    # the image installs ripgrep, so the fast path is exercised there
    from groundtruth.context_engine.callers import _search_pure_python, _search_with_rg

    (tmp_path / "app.py").write_text("def caller():\n    return helper()\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.py").write_text("def vendored():\n    return helper()\n")
    (tmp_path / "notes.txt").write_text("helper() mentioned in prose\n")

    rg_hits = sorted((h.path, h.line) for h in _search_with_rg(tmp_path, "helper"))
    py_hits = sorted((h.path, h.line) for h in _search_pure_python(tmp_path, "helper"))
    assert rg_hits == py_hits == [("app.py", 2)]
