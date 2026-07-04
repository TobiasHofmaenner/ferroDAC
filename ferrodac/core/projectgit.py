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
import re
import subprocess

log = logging.getLogger(__name__)

_FMT = "%H%x1f%an%x1f%at%x1f%s"          # sha, author, unix-time, subject (US-separated)

# userinfo (user[:pass]@) in an http(s) URL — a credential baked into a remote URL
_USERINFO = re.compile(r"^(https?://)[^/@]*@", re.IGNORECASE)


def strip_credentials(url: str) -> str:
    """Drop any `user:token@` userinfo from an http(s) remote URL, leaving the bare
    `https://host/path` — so a credential never lands in .git/config, project.json,
    a backup, or a zip. The token is injected ephemerally at git time instead."""
    return _USERINFO.sub(r"\1", url or "")


def _credential_helper(cred) -> tuple:
    """Build the `-c` args + extra env that feed git an HTTPS credential for ONE
    command via a helper that reads the secret from the ENVIRONMENT — so the token
    never appears in argv (visible to `ps`), in .git/config, or on disk. `cred` is
    `(username, password)` or None → ([], {})."""
    if not cred or not cred[1]:
        return [], {}
    user, password = cred
    helper = ('!f() { test "$1" = get && '
              'printf "username=%s\\npassword=%s\\n" "$FD_GIT_USER" "$FD_GIT_PASS"; }; f')
    args = ["-c", "credential.helper=",              # clear any inherited helper
            "-c", f"credential.helper={helper}",
            "-c", "credential.useHttpPath=false"]
    return args, {"FD_GIT_USER": user or "", "FD_GIT_PASS": password or ""}


class ProjectRepo:
    """A thin git wrapper over a project directory."""

    def __init__(self, path: str):
        self.path = path

    # -- low-level -----------------------------------------------------------
    def _git(self, *args, check=True, timeout=30, extra_env=None):
        # GIT_TERMINAL_PROMPT=0 → never block on a credential prompt (fail fast instead);
        # a default 30 s ceiling so a local op (add/status/commit) can't hang the caller
        # forever (a locked/huge repo). Network ops (push) pass a larger explicit timeout.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(extra_env or {})}
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

    def commit(self, message: str, author=None):
        """Stage everything and commit if there's anything to commit. `author` is an
        optional (name, email) attributing the commit to the real user (else the repo's
        configured identity). Returns the new sha, or None (nothing to commit / failure)."""
        from .projects import unsafe_project_dir
        reason = unsafe_project_dir(self.path)      # never git a system/home ROOT —
        if reason:                                  # `git add -A` would scan the whole tree
            log.warning("refusing git in unsafe project dir %s: %s", self.path, reason)
            return None
        try:
            self.init()
            self._git("add", "-A")
            if not self._git("status", "--porcelain").stdout.strip():
                return None                     # clean → nothing to record
            pre = []
            if author and author[0] and author[1]:
                pre = ["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}"]
            self._git(*pre, "commit", "-q", "-m", message or "checkpoint")
            return self._git("rev-parse", "HEAD").stdout.strip()
        except FileNotFoundError:
            log.warning("git not installed — project history disabled")
            return None
        except Exception as exc:                # noqa: BLE001 — never break the app
            log.warning("project commit failed in %s: %s", self.path, exc)
            return None

    def bundle(self, dest: str) -> bool:
        """Pack the WHOLE repo (all refs + full history) into a single bundle file at
        `dest` — a self-contained git repo in one file, for the archival backup
        (DESIGN §20.2). Returns False if git is missing or nothing is committed yet."""
        if not self.is_repo():
            return False
        try:
            self._git("bundle", "create", dest, "--all")
            return True
        except FileNotFoundError:
            log.warning("git not installed — history bundle skipped")
            return False
        except Exception as exc:                # noqa: BLE001 — empty repo / failure
            log.warning("project bundle failed in %s: %s", self.path, exc)
            return False

    # -- remote (push / pull to any git URL — the "native" dial) -------------
    def remote_url(self) -> str:
        if not self.is_repo():
            return ""
        out = self._git("remote", "get-url", "origin", check=False)
        return out.stdout.strip() if out.returncode == 0 else ""

    def set_remote(self, url: str) -> None:
        """Point 'origin' at a git URL. Any embedded `user:token@` credential is
        STRIPPED before it touches .git/config — we never store secrets on disk; the
        hub credential is injected ephemerally at push/pull time instead. (SSH URLs
        and bare HTTPS pass through unchanged.)"""
        url = strip_credentials(url)
        self.init()
        if self.remote_url():
            self._git("remote", "set-url", "origin", url)
        else:
            self._git("remote", "add", "origin", url)

    def sanitize_origin(self) -> bool:
        """Self-heal: if origin still carries an embedded credential (provisioned
        before this fix, and sitting in .git/config → backups/zips), rewrite it
        credential-free. Returns True if it changed anything. Cheap + idempotent."""
        cur = self.remote_url()
        clean = strip_credentials(cur)
        if cur and clean != cur:
            self._git("remote", "set-url", "origin", clean, check=False)
            log.info("scrubbed an embedded credential from %s origin URL", self.path)
            return True
        return False

    def current_branch(self) -> str:
        out = self._git("rev-parse", "--abbrev-ref", "HEAD", check=False)
        br = out.stdout.strip() if out.returncode == 0 else ""
        return br if br and br != "HEAD" else "main"

    def push(self, cred=None):
        """Push the current branch to origin (sets upstream). `cred` is an optional,
        ephemeral `(username, password)` injected for this one command (never stored).
        Returns (ok, message)."""
        return self._remote_op("push", "-u", "origin", self.current_branch(),
                               ok_msg="Pushed.", cred=cred)

    def pull(self, cred=None):
        """Pull origin/<branch> (merge, no editor). `cred` as in `push`. Returns
        (ok, message)."""
        return self._remote_op("pull", "--no-edit", "origin", self.current_branch(),
                               ok_msg="Up to date.", cred=cred)

    def _remote_op(self, *args, ok_msg="", cred=None):
        if not self.remote_url():
            return False, "No remote set — add one first."
        cargs, cenv = _credential_helper(cred)
        try:
            r = self._git(*cargs, *args, check=False, timeout=120, extra_env=cenv)
        except FileNotFoundError:
            return False, "git is not installed"
        except subprocess.TimeoutExpired:
            return False, "Timed out (network or credentials?)."
        out = (r.stderr or r.stdout or "").strip()
        return (r.returncode == 0, out or ok_msg)

    @staticmethod
    def clone(url: str, dest: str, cred=None) -> str:
        """Clone a git URL to dest (raises on failure). For 'check out a shared
        project'. `url` is stored credential-free in the new clone's config; `cred`
        (username, password), if given, authenticates this one clone ephemerally —
        the token never lands in the clone's .git/config."""
        url = strip_credentials(url)
        cargs, cenv = _credential_helper(cred)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **cenv}
        r = subprocess.run(["git", *cargs, "clone", url, dest],
                           capture_output=True, text=True, env=env, timeout=300)
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
