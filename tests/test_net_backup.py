"""HubBackupClient maps the Backup-service protos to plain dicts and streams a download
to a file atomically. No real channel — the stub is faked."""
import pytest

pytest.importorskip("grpc")
from ferrodac.net.backup import HubBackupClient                       # noqa: E402
from ferrodac_contract.v1 import data_plane_pb2 as pb                 # noqa: E402


class _FakeStub:
    def ListBackupFolders(self, req, timeout=None):
        assert req.path == "exp"
        return pb.FolderList(folders=[
            pb.BackupFolder(name="rga", path="exp/rga", project_id="p1",
                            project_name="RGA", has_children=False)])

    def SetProjectBackup(self, req, timeout=None):
        return pb.BackupInfo(ok=True, claimed=True, folder=req.folder, detail="ok")

    def GetProjectBackup(self, req, timeout=None):
        return pb.BackupInfo(ok=True, folder="exp/rga", last_backup="2026-06-26T00:00:00+00:00")

    def DownloadProject(self, req, timeout=None):
        for part in (b"PK\x03\x04", b"data", b"end"):
            yield pb.FileChunk(data=part)


def _client():
    c = HubBackupClient.__new__(HubBackupClient)
    c.stub, c.token, c.timeout = _FakeStub(), "", 5.0
    return c


def test_list_folders_maps_to_dicts():
    assert _client().list_folders("exp") == [
        {"name": "rga", "path": "exp/rga", "project_id": "p1",
         "project_name": "RGA", "has_children": False}]


def test_set_folder_maps():
    r = _client().set_folder("p1", "exp/rga")
    assert r["ok"] and r["claimed"] and r["folder"] == "exp/rga"


def test_download_streams_to_file_atomically(tmp_path):
    dest = str(tmp_path / "p.zip")
    assert _client().download("p1", dest) == dest
    with open(dest, "rb") as f:
        assert f.read() == b"PK\x03\x04dataend"        # chunks concatenated
    assert not (tmp_path / "p.zip.part").exists()      # temp cleaned up
