"""Provision per-project git repos in a bundled Gitea — the 'transparent' dial
(DESIGN §8.2).

The hub creates an empty repo per project in Gitea (via its REST API) and returns a
token-authed clone URL that project members push/pull with — so a non-power-user never
sets up git or types a URL; the hub fills the project record's `git_remote` for them.

This is the only piece that needs a git server: Gitea runs as its own compose service,
and the hub merely TALKS to its API (it never reimplements git). Fully defensive — any
failure returns "" and the project simply has no auto-provisioned remote.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


class GiteaProvisioner:
    def __init__(self, base_url: str, token: str, org: str = "ferrodac",
                 user: str = "ferrodac", public_url: str = None):
        self.base = base_url.rstrip("/")          # API base (internal, e.g. http://gitea:3000)
        # the clone URL handed to CLIENTS must be reachable from OUTSIDE the hub's network
        self.public = (public_url or base_url).rstrip("/")
        self.token = token
        self.org = org
        self.user = user                          # the token owner (URL username)

    # -- REST ----------------------------------------------------------------
    def _api(self, method: str, path: str, body=None):
        url = f"{self.base}/api/v1{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"token {self.token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:   # noqa: S310 — our own hub→gitea
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)

    def ensure_org(self) -> None:
        try:
            self._api("POST", "/orgs", {"username": self.org})
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 422):        # already exists → fine
                raise

    def repo_exists(self, name: str) -> bool:
        try:
            self._api("GET", f"/repos/{self.org}/{urllib.parse.quote(name)}")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def ensure_repo(self, name: str) -> None:
        if not self.repo_exists(name):
            self._api("POST", f"/orgs/{self.org}/repos",
                      {"name": name, "private": True, "auto_init": False})

    def clone_url(self, name: str) -> str:
        """A token-authed, externally-reachable clone URL members push/pull with."""
        scheme, _, host = self.public.partition("://")
        cred = f"{urllib.parse.quote(self.user)}:{urllib.parse.quote(self.token)}"
        return f"{scheme}://{cred}@{host}/{self.org}/{name}.git"

    # -- the one call the hub makes ------------------------------------------
    def provision(self, project_id: str) -> str:
        """Ensure a repo for the project exists and return its authed clone URL, or ""
        on any failure (the hub then leaves git_remote unset)."""
        try:
            self.ensure_org()
            self.ensure_repo(project_id)
            return self.clone_url(project_id)
        except Exception as exc:                  # noqa: BLE001 — never break a publish
            log.warning("gitea provision failed for %s: %s", project_id, exc)
            return ""
