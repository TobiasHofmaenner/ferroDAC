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


# -- Phase 2: per-project folder pick + claim + download -----------------------
def test_set_folder_assigns_claims_and_maps(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    backup.mkdir()
    b = ProjectBackup(str(projects), str(backup))
    # assign a fresh nested folder → mapped + marker written
    res = b.set_folder("p1", "P", "experiments/rga")
    assert res["ok"] and not res["claimed"] and res["folder"] == "experiments/rga"
    assert b.folder_of("p1") == "experiments/rga"
    assert b.dest_for("p1").endswith(os.path.join("experiments", "rga"))
    # a fresh ProjectBackup rebuilds the SAME map by scanning the marker (no state file)
    assert ProjectBackup(str(projects), str(backup)).folder_of("p1") == "experiments/rga"
    # re-pointing the same project at the same folder → claim (re-attach)
    assert b.set_folder("p1", "P", "experiments/rga")["claimed"] is True


def test_set_folder_rejects_other_projects_folder(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    backup.mkdir()
    b = ProjectBackup(str(projects), str(backup))
    b.set_folder("p1", "P1", "shared")
    res = b.set_folder("p2", "P2", "shared")           # p2 tries p1's folder
    assert res["ok"] is False and "backs up" in res["detail"]


def test_set_folder_reassign_moves_and_clears_old(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    backup.mkdir()
    b = ProjectBackup(str(projects), str(backup))
    b.set_folder("p1", "P", "a")
    b.mirror("p1", "P")                                 # populate "a"
    assert (backup / "a" / "project.json").exists()
    b.set_folder("p1", "P", "b")                        # move a → b
    assert b.folder_of("p1") == "b"
    assert not (backup / "a").exists()                  # old backup removed → no double-claim
    # a fresh scan resolves to "b" only (no ambiguity)
    assert ProjectBackup(str(projects), str(backup)).folder_of("p1") == "b"


def test_set_folder_rejects_escape(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    backup.mkdir()
    b = ProjectBackup(str(projects), str(backup))
    assert b.set_folder("p1", "P", "../../etc")["ok"] is False


def test_list_folders_tags_claims(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    backup.mkdir()
    (backup / "experiments").mkdir()                    # an organizational folder
    b = ProjectBackup(str(projects), str(backup))
    b.set_folder("p1", "MyProj", "experiments/rga")
    listing = {f["name"]: f for f in b.list_folders("experiments")}
    assert listing["rga"]["project_id"] == "p1" and listing["rga"]["project_name"] == "MyProj"


def test_make_zip_download(tmp_path):
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    backup.mkdir()
    b = ProjectBackup(str(projects), str(backup))
    dest = str(tmp_path / "dl.zip")
    assert b.make_zip("p1", dest) == dest
    import zipfile
    with zipfile.ZipFile(dest) as z:
        assert "project.json" in z.namelist()
    assert b.make_zip("nope", str(tmp_path / "x.zip")) is None


def test_backup_servicer_set_get_list(tmp_path):
    import asyncio
    from ferrodac_contract.v1 import data_plane_pb2 as pb
    from hub.core import Hub
    from hub.service import BackupServicer
    projects, backup = tmp_path / "projects", tmp_path / "backup"
    _mk_project(str(projects), "p1")
    backup.mkdir()
    svc = BackupServicer(Hub(projects_dir=str(projects), backup_dir=str(backup)))

    async def run():
        r = await svc.SetProjectBackup(
            pb.SetBackupRequest(project_id="p1", folder="experiments/rga"), None)
        assert r.ok and r.folder == "experiments/rga"
        g = await svc.GetProjectBackup(pb.GetBackupRequest(project_id="p1"), None)
        assert g.folder == "experiments/rga" and g.last_backup        # mirrored just now
        lst = await svc.ListBackupFolders(pb.ListFoldersRequest(path="experiments"), None)
        assert any(f.name == "rga" and f.project_id == "p1" for f in lst.folders)

    asyncio.run(run())
    assert (backup / "experiments" / "rga" / "project.json").exists()   # mirrored content
