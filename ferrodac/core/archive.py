"""Project local backup — a single self-contained `.zip` of a project's METADATA
(docs / layouts / records / tags / bookmarks; **not** measurements, which live in Zarr
and sync via the store, §12.1). The zip holds the project's readable files **plus an
invisible `history.bundle`** — the full git history packed into one file — so recovery
is a plain ``git clone history.bundle`` done by the app (no git literacy needed).

Read-only purely by being a zip: to edit it you'd extract → edit → re-zip, and it isn't
wired back anywhere. DESIGN §20.2. Qt-free, so it's testable headless and could run
server-side.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile

from .projectgit import ProjectRepo

log = logging.getLogger("ferrodac.archive")

HISTORY_NAME = "history.bundle"


def archive_project(project, dest: str, include_history: bool = True) -> str:
    """Write a self-contained backup zip of `project` to `dest` (atomic). Returns `dest`.

    Every project file is included except ``.git/`` (replaced by the compact history
    bundle). With `include_history` and a git repo, ``history.bundle`` (full history) is
    added — so the latest readable state AND the whole history travel in one file.
    Best-effort about history: a project with no git just yields a latest-snapshot zip.
    """
    bundle_dir = None
    bundle_file = None
    if include_history:
        repo = ProjectRepo(project.path)
        if repo.is_repo():
            bundle_dir = tempfile.mkdtemp(prefix="ferrodac-bundle-")
            cand = os.path.join(bundle_dir, HISTORY_NAME)
            if repo.bundle(cand) and os.path.exists(cand):
                bundle_file = cand

    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(project.path):
                if ".git" in dirs:
                    dirs.remove(".git")             # carried compactly by the bundle
                for name in files:
                    full = os.path.join(root, name)
                    if os.path.isfile(full):        # skip broken symlinks / sockets
                        z.write(full, os.path.relpath(full, project.path))
            if bundle_file:
                z.write(bundle_file, HISTORY_NAME)
        os.replace(tmp, dest)                        # atomic publish
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        if bundle_dir and os.path.isdir(bundle_dir):
            shutil.rmtree(bundle_dir, ignore_errors=True)
    return dest
