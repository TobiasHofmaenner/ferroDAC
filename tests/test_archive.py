"""Project local backup (DESIGN §20.2): a self-contained zip = readable files +
an invisible history.bundle, '.git' excluded, recoverable by cloning the bundle.
Qt-free."""
import shutil
import subprocess
import types
import zipfile

import pytest

from ferrodac.core.archive import archive_project

_HAS_GIT = shutil.which("git") is not None
gitonly = pytest.mark.skipif(not _HAS_GIT, reason="git not installed")


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True)


def _make_repo(path):
    path.mkdir(parents=True)
    (path / "project.json").write_text('{"name": "P"}')
    (path / "docs").mkdir()
    (path / "docs" / "readme.md").write_text("# hi")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


@gitonly
def test_archive_contains_files_and_history(tmp_path):
    proj = tmp_path / "proj"
    _make_repo(proj)
    dest = str(tmp_path / "backup.zip")
    archive_project(types.SimpleNamespace(path=str(proj)), dest)
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
    assert "project.json" in names                  # readable files…
    assert "docs/readme.md" in names
    assert "history.bundle" in names                # …+ the hidden history
    assert not any(n.startswith(".git/") for n in names)   # .git itself excluded


@gitonly
def test_history_bundle_recovers_a_full_clone(tmp_path):
    proj = tmp_path / "proj"
    _make_repo(proj)
    dest = str(tmp_path / "backup.zip")
    archive_project(types.SimpleNamespace(path=str(proj)), dest)
    # the recovery path: pull the one bundle out and clone it back into a live repo
    with zipfile.ZipFile(dest) as z:
        z.extract("history.bundle", tmp_path)
    recovered = tmp_path / "recovered"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "history.bundle"),
                    str(recovered)], check=True, capture_output=True)
    assert (recovered / "project.json").exists()
    assert (recovered / "docs" / "readme.md").read_text() == "# hi"


def test_archive_without_git_is_latest_snapshot(tmp_path):
    proj = tmp_path / "p2"
    proj.mkdir()
    (proj / "a.json").write_text("{}")
    dest = str(tmp_path / "b2.zip")
    archive_project(types.SimpleNamespace(path=str(proj)), dest)
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
    assert "a.json" in names and "history.bundle" not in names   # graceful fallback


@gitonly
def test_archive_is_atomic_overwrite(tmp_path):
    """Re-archiving replaces the file in place and leaves no .tmp behind."""
    proj = tmp_path / "proj"
    _make_repo(proj)
    dest = tmp_path / "backup.zip"
    archive_project(types.SimpleNamespace(path=str(proj)), str(dest))
    (proj / "new.md").write_text("added")
    archive_project(types.SimpleNamespace(path=str(proj)), str(dest))
    with zipfile.ZipFile(dest) as z:
        assert "new.md" in z.namelist()
    assert not (tmp_path / "backup.zip.tmp").exists()
