"""Local git history for a project folder (DESIGN §8.2, the "clients are git clients"
foundation).

A project's *bytes* — reports, layouts, docs, exported CSVs, papers — live in its
folder; this versions them with git. Measurements are NOT here (they stay in the Zarr
data plane). Commits happen at BOUNDARIES (a recording saved, a named layout, a manual
checkpoint, settled doc edits), never per keystroke.

Everything is defensive: git missing or a failure never raises into the app — a commit
just doesn't happen.
"""
from __future__ import annotations

import getpass
import logging
import os
import subprocess

log = logging.getLogger(__name__)

_FMT = "%H%x1f%an%x1f%at%x1f%s"          # sha, author, unix-time, subject (US-separated)


class ProjectRepo:
    """A thin git wrapper over a project directory."""

    def __init__(self, path: str):
        self.path = path

    # -- low-level -----------------------------------------------------------
    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", self.path, *args], check=check,
                              capture_output=True, text=True)

    def is_repo(self) -> bool:
        return os.path.isdir(os.path.join(self.path, ".git"))

    def init(self) -> None:
        """Create the repo if absent, with a sensible local identity (so commits work
        even with no global git config)."""
        if self.is_repo():
            return
        os.makedirs(self.path, exist_ok=True)
        self._git("init", "-q")
        user = (getpass.getuser() or "ferroDAC").strip() or "ferroDAC"
        self._git("config", "user.name", user)
        self._git("config", "user.email", f"{user}@ferrodac.local")

    # -- high-level ----------------------------------------------------------
    def is_dirty(self) -> bool:
        """True if there are uncommitted changes (or nothing committed yet)."""
        if not self.is_repo():
            return os.path.isdir(self.path) and bool(os.listdir(self.path))
        try:
            return bool(self._git("status", "--porcelain").stdout.strip())
        except Exception:                       # noqa: BLE001
            return False

    def commit(self, message: str):
        """Stage everything and commit if there's anything to commit. Returns the new
        commit sha, or None (nothing to commit / git unavailable / failure)."""
        try:
            self.init()
            self._git("add", "-A")
            if not self._git("status", "--porcelain").stdout.strip():
                return None                     # clean → nothing to record
            self._git("commit", "-q", "-m", message or "checkpoint")
            return self._git("rev-parse", "HEAD").stdout.strip()
        except FileNotFoundError:
            log.warning("git not installed — project history disabled")
            return None
        except Exception as exc:                # noqa: BLE001 — never break the app
            log.warning("project commit failed in %s: %s", self.path, exc)
            return None

    def log(self, limit: int = 100) -> list:
        """Recent history: [{sha, author, time, message}] (newest first)."""
        if not self.is_repo():
            return []
        try:
            out = self._git("log", f"-{int(limit)}", f"--pretty=format:{_FMT}",
                            check=False).stdout
        except Exception:                       # noqa: BLE001
            return []
        rows = []
        for line in out.splitlines():
            p = line.split("\x1f")
            if len(p) == 4:
                rows.append({"sha": p[0], "author": p[1],
                             "time": int(p[2]) if p[2].isdigit() else 0, "message": p[3]})
        return rows
