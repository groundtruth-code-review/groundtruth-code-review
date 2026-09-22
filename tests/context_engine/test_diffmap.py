from groundtruth.context_engine.diffmap import parse_diff, render_file_diff

SIMPLE_DIFF = """diff --git a/app.py b/app.py
index 0000000..1111111 100644
--- a/app.py
+++ b/app.py
@@ -10,3 +10,4 @@ def handler():
 def handler():
     x = 1
-    return x
+    return x + 1
+    # trailing comment
"""


def test_parses_file_path():
    dm = parse_diff(SIMPLE_DIFF)
    assert "app.py" in dm


def test_tracks_added_line_numbers():
    dm = parse_diff(SIMPLE_DIFF)
    # context line 10 ("def handler():"), context line 11 ("x = 1"),
    # then one removed + two added starting at new-file line 12.
    assert dm["app.py"].added == {12, 13}


def test_hunk_metadata():
    dm = parse_diff(SIMPLE_DIFF)
    hunks = dm["app.py"].hunks
    assert len(hunks) == 1
    h = hunks[0]
    assert h.old_start == 10 and h.new_start == 10
    assert h.removed_count == 1


def test_multiple_files_in_one_diff():
    text = SIMPLE_DIFF + (
        "diff --git a/other.py b/other.py\n"
        "index 0000000..2222222 100644\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -1,1 +1,2 @@\n"
        " first line\n"
        "+second line\n"
    )
    dm = parse_diff(text)
    assert set(dm.keys()) == {"app.py", "other.py"}
    assert dm["other.py"].added == {2}


def test_pure_deletion_is_ignored_not_crashed():
    text = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-line one\n"
        "-line two\n"
    )
    dm = parse_diff(text)
    assert dm == {}


def test_garbage_input_never_raises():
    assert parse_diff("this is not a diff at all\njust some text") == {}
    assert parse_diff("") == {}


def test_render_file_diff_reconstructs_the_hunk_body():
    dm = parse_diff(SIMPLE_DIFF)
    rendered = render_file_diff("app.py", dm["app.py"])
    assert "--- a/app.py" in rendered
    assert "+++ b/app.py" in rendered
    assert "+    return x + 1" in rendered
    assert "-    return x" in rendered


def test_render_file_diff_isolates_one_file_from_a_multi_file_diff():
    text = SIMPLE_DIFF + (
        "diff --git a/other.py b/other.py\n"
        "index 0000000..2222222 100644\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -1,1 +1,2 @@\n"
        " first line\n"
        "+totally unrelated addition\n"
    )
    dm = parse_diff(text)
    rendered = render_file_diff("app.py", dm["app.py"])
    assert "totally unrelated addition" not in rendered
    assert "+++ b/other.py" not in rendered
