"""Server-side project backups — the hub is the authoritative writer (DESIGN §20).

The hub already keeps each project as a real folder (``core.projects.Project`` layout)
under its ``projects_dir``. This mirrors each project ONE-WAY and INCREMENTALLY into a
configured backend directory (a server path now; a mounted Nextcloud folder later): only
changed files are copied and vanished ones removed, so a one-byte README edit moves one
byte — not the whole archive.

The mirror is **server output, never an editing surface**, so there is no conflict
resolution: a stray edit in the backend is simply overwritten next cycle (the project,
edited through the app, is authoritative). On a Nextcloud backend the folder can also be
shared read-only as a UX nicety — an adapter concern, never a correctness dependency.

Qt-free. Best-effort: a backup hiccup must never affect the hub.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading

log = logging.getLogger("hub.backup")

MARKER = "ferrodac-project.json"          # self-identifying; survives the strict mirror


class ProjectBackup:
    """Mirrors hub project folders (``projects_dir/<id>``) into ``backup_dir/<id>/``."""

    def __init__(self, projects_dir: str, backup_dir: str) -> None:
        self.projects_dir = projects_dir
        self.backup_dir = backup_dir
        self._locks: dict[str, threading.Lock] = {}
        self._gate = threading.Lock()

    def _plock(self, pid: str) -> threading.Lock:
        with self._gate:
            return self._locks.setdefault(pid, threading.Lock())

    def dest_for(self, pid: str) -> str:
        # Phase 1: key the backend folder by project id (collision-free). Phase 2 lets
        # the operator pick a human-named folder + claim an existing one.
        return os.path.join(self.backup_dir, pid)

    def mirror(self, pid: str, name: str = "") -> bool:
        """Incrementally mirror project `pid` to the backend. Returns True if anything
        changed. Never raises."""
        src = os.path.join(self.projects_dir, pid)
        if not os.path.isdir(src):
            return False
        dst = self.dest_for(pid)
        try:
            with self._plock(pid):
                os.makedirs(dst, exist_ok=True)
                changed = _mirror_tree(src, dst, keep={MARKER})
                self._write_marker(dst, pid, name)
            return changed
        except Exception as exc:                     # noqa: BLE001 — never break the hub
            log.warning("backup mirror failed for %s: %s", pid, exc)
            return False

    @staticmethod
    def _write_marker(dst: str, pid: str, name: str) -> None:
        body = json.dumps({"id": pid, "name": name}, indent=2)
        path = os.path.join(dst, MARKER)
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == body:
                    return                           # unchanged → don't churn it
        except OSError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)


def _mirror_tree(src: str, dst: str, keep=frozenset()) -> bool:
    """One-way incremental mirror src→dst: copy new/changed files, delete vanished ones
    (except names in `keep`), excluding ``.git/``. Returns True if anything changed."""
    changed = False
    wanted: dict[str, str] = {}
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            full = os.path.join(root, f)
            if os.path.isfile(full):
                wanted[os.path.relpath(full, src)] = full
    for rel, full in wanted.items():
        target = os.path.join(dst, rel)
        if _differs(full, target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(full, target)               # copy2 preserves mtime → no re-copy
            changed = True
    for root, dirs, files in os.walk(dst):           # strict mirror: drop what's gone
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), dst)
            if rel in keep or rel in wanted:
                continue
            os.remove(os.path.join(root, f))
            changed = True
    _prune_empty_dirs(dst)
    return changed


def _differs(src: str, dst: str) -> bool:
    try:
        ss, ds = os.stat(src), os.stat(dst)
    except OSError:
        return True
    return ss.st_size != ds.st_size or int(ss.st_mtime) != int(ds.st_mtime)


def _prune_empty_dirs(root: str) -> None:
    for cur, _dirs, _files in os.walk(root, topdown=False):
        if cur == root:
            continue
        try:
            if not os.listdir(cur):
                os.rmdir(cur)
        except OSError:
            pass
