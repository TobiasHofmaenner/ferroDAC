"""HubBackupClient — the hub's Backup service (DESIGN §20): browse the backend folder
tree, point a project at a folder (claiming an existing backup when the marker matches),
and download a project as a self-contained zip.

Synchronous gRPC (like HubReadTier) — call it from a worker thread or behind a wait
cursor. Degrades to absent if grpcio is missing.
"""
from __future__ import annotations

import logging
import os

from . import GRPC_AVAILABLE

log = logging.getLogger("hub.backup")
_TIMEOUT = 10.0

if GRPC_AVAILABLE:
    from ferrodac_contract.v1 import data_plane_pb2 as pb
    from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc


class HubBackupClient:
    def __init__(self, channel, token: str = "", timeout: float = _TIMEOUT):
        self.stub = rpc.BackupStub(channel)
        self.token = token
        self.timeout = timeout

    def list_folders(self, path: str = "") -> list:
        """Subfolders under `path` (relative to the backend root), each tagged with the
        project it backs up (project_id/name) and whether it has children to drill into."""
        resp = self.stub.ListBackupFolders(
            pb.ListFoldersRequest(token=self.token, path=path), timeout=self.timeout)
        return [{"name": f.name, "path": f.path, "project_id": f.project_id,
                 "project_name": f.project_name, "has_children": f.has_children}
                for f in resp.folders]

    def set_folder(self, project_id: str, folder: str) -> dict:
        """Point `project_id`'s backup at `folder`. Returns {ok, claimed, folder, detail}."""
        r = self.stub.SetProjectBackup(
            pb.SetBackupRequest(token=self.token, project_id=project_id, folder=folder),
            timeout=self.timeout)
        return {"ok": r.ok, "claimed": r.claimed, "folder": r.folder,
                "detail": r.detail, "last_backup": r.last_backup}

    def get_folder(self, project_id: str) -> dict:
        r = self.stub.GetProjectBackup(
            pb.GetBackupRequest(token=self.token, project_id=project_id),
            timeout=self.timeout)
        return {"ok": r.ok, "folder": r.folder, "last_backup": r.last_backup}

    def download(self, project_id: str, dest: str) -> str:
        """Stream the project's zip to `dest` (atomic via a .part file). Returns dest."""
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as fh:
                for chunk in self.stub.DownloadProject(
                        pb.DownloadRequest(token=self.token, project_id=project_id),
                        timeout=max(self.timeout, 120.0)):
                    fh.write(chunk.data)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return dest
