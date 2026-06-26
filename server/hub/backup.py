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
    """Mirrors hub project folders (``projects_dir/<id>``) into the backend.

    The per-project backend SUBFOLDER is chosen by the client (Phase 2) and recorded by
    a ``ferrodac-project.json`` marker in that folder — so the ``id -> folder`` map is
    rebuilt by SCANNING markers, never a separate state file. Unmapped projects default
    to ``backup_dir/<id>/`` (Phase 1)."""

    def __init__(self, projects_dir: str, backup_dir: str) -> None:
        self.projects_dir = projects_dir
        self.backup_dir = backup_dir
        self._locks: dict[str, threading.Lock] = {}
        self._gate = threading.Lock()
        self._map: dict[str, str] = {}          # project id -> folder (relative to root)
        self._refresh_map()

    def _plock(self, pid: str) -> threading.Lock:
        with self._gate:
            return self._locks.setdefault(pid, threading.Lock())

    # -- folder map (rebuilt by scanning markers) ----------------------------
    def _refresh_map(self) -> None:
        found: dict[str, str] = {}
        for cur, dirs, files in os.walk(self.backup_dir):
            if MARKER in files:
                m = _read_marker(os.path.join(cur, MARKER))
                pid = m.get("id")
                if pid:
                    found.setdefault(pid, os.path.relpath(cur, self.backup_dir))
                dirs[:] = []                    # a project backup — don't recurse below it
        with self._gate:
            self._map = found

    def folder_of(self, pid: str) -> str:
        with self._gate:
            return self._map.get(pid, "")

    def dest_for(self, pid: str) -> str:
        rel = self.folder_of(pid) or pid        # mapped folder, else default <id>
        return os.path.join(self.backup_dir, rel)

    def last_backup(self, pid: str) -> str:
        """ISO-8601 UTC mtime of the project's marker (when it last mirrored), '' if never."""
        marker = os.path.join(self.dest_for(pid), MARKER)
        try:
            ts = os.path.getmtime(marker)
        except OSError:
            return ""
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")

    # -- picker / claim (Phase 2) --------------------------------------------
    def _norm_rel(self, relpath: str):
        rel = os.path.normpath((relpath or "").strip().lstrip("/\\"))
        if rel in (".", ""):
            return ""
        if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
            return None                          # escape attempt → reject
        abs_target = os.path.abspath(os.path.join(self.backup_dir, rel))
        if not abs_target.startswith(os.path.abspath(self.backup_dir) + os.sep):
            return None                          # outside the root
        return rel

    def list_folders(self, relpath: str = "") -> list:
        """Subfolders under `relpath` (relative to the root) for the picker, each tagged
        with the project it backs up (if any) and whether it has sub-folders to drill into."""
        rel = self._norm_rel(relpath)
        if rel is None:
            return []
        base = os.path.join(self.backup_dir, rel)
        out = []
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return []
        for name in names:
            full = os.path.join(base, name)
            if not os.path.isdir(full):
                continue
            m = _read_marker(os.path.join(full, MARKER))
            try:
                kids = any(os.path.isdir(os.path.join(full, c)) for c in os.listdir(full))
            except OSError:
                kids = False
            out.append({"name": name, "path": (os.path.join(rel, name) if rel else name),
                        "project_id": m.get("id", ""), "project_name": m.get("name", ""),
                        "has_children": kids and not m})   # don't drill into a project backup
        return out

    def set_folder(self, pid: str, name: str, folder: str) -> dict:
        """Point project `pid`'s backup at `folder` (relative to the root). Empty/new →
        assign; an existing marker for THIS project → claim (re-attach); a marker for a
        DIFFERENT project → reject (never clobber). Returns {ok, claimed, detail, folder}."""
        rel = self._norm_rel(folder)
        if rel is None or rel == "":
            return {"ok": False, "claimed": False, "folder": "",
                    "detail": "Pick a folder inside the backup area."}
        target = os.path.join(self.backup_dir, rel)
        marker = os.path.join(target, MARKER)
        claimed = False
        if os.path.isfile(marker):
            m = _read_marker(marker)
            if m.get("id") and m.get("id") != pid:
                return {"ok": False, "claimed": False, "folder": "",
                        "detail": f"That folder already backs up “{m.get('name') or m['id']}”."}
            claimed = True
        elif os.path.isdir(target) and _has_content(target):
            return {"ok": False, "claimed": False, "folder": "",
                    "detail": "That folder isn't empty and isn't a ferroDAC backup."}
        old = self.folder_of(pid)               # reassigning? drop the old marker (no double-claim)
        if old and self._norm_rel(old) != rel:
            try:
                os.remove(os.path.join(self.backup_dir, old, MARKER))
            except OSError:
                pass
        os.makedirs(target, exist_ok=True)
        self._write_marker(target, pid, name)
        self._refresh_map()
        return {"ok": True, "claimed": claimed, "folder": rel,
                "detail": "Re-attached to the existing backup." if claimed
                          else "Backup folder set."}

    def make_zip(self, pid: str, dest: str):
        """Generate a self-contained download zip of a project (files + history.bundle)
        from the hub's project folder. Returns dest, or None if unknown."""
        src = os.path.join(self.projects_dir, pid)
        if not os.path.isdir(src):
            return None
        import types
        from ferrodac.core.archive import archive_project
        return archive_project(types.SimpleNamespace(path=src), dest)

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


def _read_marker(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _has_content(path: str) -> bool:
    """True if the dir holds anything other than our marker (so we never pollute it)."""
    try:
        return any(n != MARKER for n in os.listdir(path))
    except OSError:
        return False


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
