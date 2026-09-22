from groundtruth.context_engine.chunker import enclosing_chunks, find_by_name, parse_file

PY_SRC = """class Bar:
    def method(self, x):
        return x * 2

def foo(a, b):
    result = Bar().method(a)
    return result + b

def caller_of_foo():
    return foo(1, 2)
"""


def test_parses_top_level_functions_and_methods():
    chunks = parse_file(PY_SRC, "sample.py")
    names = {c.name for c in chunks}
    assert names == {"method", "foo", "caller_of_foo"}


def test_line_ranges_are_one_indexed_inclusive():
    chunks = parse_file(PY_SRC, "sample.py")
    foo = find_by_name(chunks, "foo")
    assert foo is not None
    assert foo.start_line == 5  # "def foo(a, b):"
    assert foo.end_line == 7  # "    return result + b"


def test_enclosing_chunk_picks_the_innermost():
    chunks = parse_file(PY_SRC, "sample.py")
    # line 2 is inside Bar.method, not just "inside Bar"
    enclosing = enclosing_chunks(chunks, {2})
    assert [c.name for c in enclosing] == ["method"]


def test_enclosing_chunk_for_multiple_lines_across_functions():
    chunks = parse_file(PY_SRC, "sample.py")
    enclosing = enclosing_chunks(chunks, {2, 6})
    assert {c.name for c in enclosing} == {"method", "foo"}


def test_header_is_the_signature_line():
    chunks = parse_file(PY_SRC, "sample.py")
    foo = find_by_name(chunks, "foo")
    assert foo.header == "def foo(a, b):"


def test_javascript_also_works():
    js_src = "function foo(a, b) {\n  return a + b;\n}\n"
    chunks = parse_file(js_src, "sample.js")
    assert [c.name for c in chunks] == ["foo"]


def test_unknown_extension_degrades_to_empty_not_crash():
    assert parse_file("whatever content", "sample.xyz123") == []


def test_unparseable_source_degrades_gracefully():
    # Garbage Python — tree-sitter still produces a (possibly broken) parse
    # tree rather than raising, and worst case we just get fewer/no chunks.
    result = parse_file("def (((( not valid python at all", "broken.py")
    assert isinstance(result, list)
