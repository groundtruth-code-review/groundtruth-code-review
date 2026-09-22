import subprocess

import pytest

from groundtruth.git_source import GitError, git_diff, load_sources, merge_base, show_file


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "invoice.py").write_text("def calculate_discount(price):\n    return price * 0.9\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    (tmp_path / "invoice.py").write_text(
        "def calculate_discount(price, promos):\n"
        "    rate = sum(p.rate for p in promos)\n"
        "    return price * rate\n"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "head")

    return tmp_path, base_sha


def test_git_diff_shows_the_actual_change(repo):
    repo_path, base_sha = repo
    diff = git_diff(repo_path, base_sha, "HEAD")
    assert "calculate_discount" in diff
    assert "+    rate = sum(p.rate for p in promos)" in diff


def test_show_file_at_a_ref(repo):
    repo_path, base_sha = repo
    content = show_file(repo_path, base_sha, "invoice.py")
    assert content == "def calculate_discount(price):\n    return price * 0.9\n"


def test_show_file_missing_at_ref_returns_none_not_raise(repo):
    repo_path, base_sha = repo
    assert show_file(repo_path, base_sha, "does_not_exist.py") is None


def test_load_sources_skips_missing_paths(repo):
    repo_path, base_sha = repo
    sources = load_sources(repo_path, base_sha, ["invoice.py", "nope.py"])
    assert set(sources.keys()) == {"invoice.py"}


def test_load_sources_at_head(repo):
    repo_path, base_sha = repo
    sources = load_sources(repo_path, "HEAD", ["invoice.py"])
    assert "promos" in sources["invoice.py"]


def test_merge_base_is_the_common_ancestor(repo):
    repo_path, base_sha = repo
    assert merge_base(repo_path, base_sha, "HEAD") == base_sha


def test_git_diff_raises_gitError_on_bad_refs(repo):
    repo_path, _ = repo
    with pytest.raises(GitError):
        git_diff(repo_path, "not-a-real-ref", "also-not-real")
