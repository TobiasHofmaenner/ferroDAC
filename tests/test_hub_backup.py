"""Server-side project backup (DESIGN §20): the hub mirrors project folders ONE-WAY
and INCREMENTALLY into a backend dir — only changed files move, vanished ones drop,
.git is excluded, and a self-identifying marker is written. No conflict resolution:
it's server output, overwritten next cycle. Qt-free."""
import json
import os
import time

from hub.backup import MARKER, ProjectBackup


def _mk_project(projects_dir, pid):
    d = os.path.join(projects_dir, pid)
    os.makedirs(os.path.join(d, "docs"))
    with open(os.path.join(d, "project.json"), "w") as f:
        json.dump({"id": pid, "name": "P"}, f)        # _META → a valid project folder
    with open(os.path.join(d, "docs", "readme.md"), "w") as f:
        f.write("# hi")
    os.makedirs(os.path.join(d, ".git"))              # must be excluded from the mirror
    with open(os.path.join(d, ".git", "config"), "w") as f:
        f.write("x")
    return d


def test_mirror_copies_files_and_marker_excludes_git(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    b = ProjectBackup(str(projects), str(backup))
    assert b.mirror("p1", "P") is True
    dst = backup / "p1"
    assert (dst / "project.json").exists()
    assert (dst / "docs" / "readme.md").read_text() == "# hi"
    assert not (dst / ".git").exists()                # .git excluded
    assert json.loads((dst / MARKER).read_text()) == {"id": "p1", "name": "P"}


def test_mirror_is_incremental(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    d = _mk_project(str(projects), "p1")
    b = ProjectBackup(str(projects), str(backup))
    b.mirror("p1", "P")
    dst = backup / "p1"
    untouched_mtime = (dst / "project.json").stat().st_mtime
    time.sleep(1.1)                                   # clear the 1-second mtime granularity
    with open(os.path.join(d, "docs", "readme.md"), "w") as f:
        f.write("# changed")
    assert b.mirror("p1", "P") is True
    assert (dst / "docs" / "readme.md").read_text() == "# changed"
    # the unchanged file was NOT recopied (incremental) — its backend mtime is intact
    assert (dst / "project.json").stat().st_mtime == untouched_mtime
    assert b.mirror("p1", "P") is False               # nothing changed → a no-op


def test_mirror_deletes_vanished_keeps_marker(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    d = _mk_project(str(projects), "p1")
    b = ProjectBackup(str(projects), str(backup))
    b.mirror("p1", "P")
    os.remove(os.path.join(d, "docs", "readme.md"))
    assert b.mirror("p1", "P") is True
    dst = backup / "p1"
    assert not (dst / "docs" / "readme.md").exists()  # strict mirror drops it…
    assert (dst / "project.json").exists()
    assert (dst / MARKER).exists()                    # …but the marker survives


def test_mirror_missing_project_is_noop(tmp_path):
    b = ProjectBackup(str(tmp_path / "projects"), str(tmp_path / "backup"))
    assert b.mirror("nope", "X") is False


def test_hub_flushes_queued_backups(tmp_path):
    from hub.core import Hub
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    hub = Hub(projects_dir=str(projects), backup_dir=str(backup))   # _load_projects → dirty
    hub.flush_backups()
    assert (backup / "p1" / "project.json").exists()
    assert hub.flush_backups() is None and not (backup / "p1.tmp").exists()
