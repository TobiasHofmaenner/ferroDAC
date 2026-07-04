"""Fetch a project's ephemeral git push/pull credential from the hub
(Projects.GetGitCredential).

The shared record's `git_remote` is credential-free (the token used to be baked into
it and leaked into project.json / backups / zips — a hub-wide admin credential in
every clone). The push/pull token is fetched on demand over this authenticated call,
held in memory, and injected at git time via a credential helper — never persisted.
"""
from __future__ import annotations

import logging

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

log = logging.getLogger("hub.gitcred")

_TIMEOUT = 15.0


class HubGitCredentialClient:
    def __init__(self, channel, token: str = "", timeout: float = _TIMEOUT):
        self.stub = rpc.ProjectsStub(channel)
        self.token = token
        self.timeout = timeout

    def get(self, project_id: str):
        """Return `(url, username, password)` for pushing/pulling the project's
        provisioned repo, or None when it has no transparent-git repo (native dial /
        unknown project / Gitea off) or the hub is unreachable."""
        try:
            resp = self.stub.GetGitCredential(
                pb.GitCredentialRequest(token=self.token, project_id=project_id),
                timeout=self.timeout)
        except Exception as exc:                  # noqa: BLE001 — defensive, never raise
            log.warning("git credential fetch failed for %s: %s", project_id, exc)
            return None
        return (resp.url, resp.username, resp.password) if resp.ok else None
