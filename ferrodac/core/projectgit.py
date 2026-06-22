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
    def _git(self, *args, check=True, timeout=None):
        # GIT_TERMINAL_PROMPT=0 → never block on a credential prompt (fail fast instead)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(["git", "-C", self.path, *args], check=check,
                              capture_output=True, text=True, env=env, timeout=timeout)

    def is_repo(self) -> bool:
        return os.path.isdir(os.path.join(self.path, ".git"))

    def init(self) -> None:
        """Create the repo if absent, and ensure a commit identity (so commits work
        even with no global git config — including in a freshly cloned project)."""
        if not self.is_repo():
            os.makedirs(self.path, exist_ok=True)
            self._git("init", "-q", "-b", "main")   # consistent branch name for push/pull
        if not self._git("config", "user.name", check=False).stdout.strip():
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

    # -- remote (push / pull to any git URL — the "native" dial) -------------
    def remote_url(self) -> str:
        if not self.is_repo():
            return ""
        out = self._git("remote", "get-url", "origin", check=False)
        return out.stdout.strip() if out.returncode == 0 else ""

    def set_remote(self, url: str) -> None:
        """Point 'origin' at a git URL (HTTPS with a token, or SSH). Credentials are
        the user's git setup (token-in-URL / credential helper / SSH key) — we don't
        store secrets."""
        self.init()
        if self.remote_url():
            self._git("remote", "set-url", "origin", url)
        else:
            self._git("remote", "add", "origin", url)

    def current_branch(self) -> str:
        out = self._git("rev-parse", "--abbrev-ref", "HEAD", check=False)
        br = out.stdout.strip() if out.returncode == 0 else ""
        return br if br and br != "HEAD" else "main"

    def push(self):
        """Push the current branch to origin (sets upstream). Returns (ok, message)."""
        return self._remote_op("push", "-u", "origin", self.current_branch(),
                               ok_msg="Pushed.")

    def pull(self):
        """Pull origin/<branch> (merge, no editor). Returns (ok, message)."""
        return self._remote_op("pull", "--no-edit", "origin", self.current_branch(),
                               ok_msg="Up to date.")

    def _remote_op(self, *args, ok_msg=""):
        if not self.remote_url():
            return False, "No remote set — add one first."
        try:
            r = self._git(*args, check=False, timeout=120)
        except FileNotFoundError:
            return False, "git is not installed"
        except subprocess.TimeoutExpired:
            return False, "Timed out (network or credentials?)."
        out = (r.stderr or r.stdout or "").strip()
        return (r.returncode == 0, out or ok_msg)

    @staticmethod
    def clone(url: str, dest: str) -> str:
        """Clone a git URL to dest (raises on failure). For 'check out a shared project'."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        r = subprocess.run(["git", "clone", url, dest], capture_output=True, text=True,
                           env=env, timeout=300)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "clone failed").strip())
        return dest

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
