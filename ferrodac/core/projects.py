"""Projects — a curation / filter overlay on the global data plane (DESIGN §8.1).

A project does NOT isolate data: the whole catalog + history stay global and
fully accessible. A project is a FOLDER that *groups* the things that are
genuinely project-specific — analysis layouts, docs, reports, favourites, and
(later) a tag lens — so you can order your work and not drown in 1000 overlapping
tags from other experiments.

Filesystem is the source of truth for collections: `project.json` holds **meta
only**; the contents of `layouts/` (etc.) ARE the list — scan them, never mirror
them into the json (no sync burden). Qt-free + testable.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone

PROJECT_VERSION = 1
_META = "project.json"
_SUBDIRS = ("layouts", "docs", "reports")


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", str(name)).strip("_") or "project"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_project(path: str) -> bool:
    return os.path.isfile(os.path.join(path, _META))


class Project:
    """One project folder: `project.json` (meta) + layouts/ docs/ reports/."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.meta: dict = {}
        self._load()

    # -- identity / meta -----------------------------------------------------
    @property
    def id(self) -> str:
        return self.meta.get("id", "")

    @property
    def name(self) -> str:
        return self.meta.get("name") or os.path.basename(self.path)

    @property
    def description(self) -> str:
        return self.meta.get("description", "")

    def _load(self) -> None:
        try:
            with open(os.path.join(self.path, _META), encoding="utf-8") as fh:
                self.meta = json.load(fh)
        except Exception:
            self.meta = {}

    def save(self) -> None:
        os.makedirs(self.path, exist_ok=True)
        self.meta["modified"] = _now()
        with open(os.path.join(self.path, _META), "w", encoding="utf-8") as fh:
            json.dump(self.meta, fh, indent=2)

    def set_meta(self, **fields) -> None:
        self.meta.update(fields)
        self.save()

    # -- folders (scanned; the folder IS the list) ---------------------------
    def subdir(self, name: str) -> str:
        d = os.path.join(self.path, name)
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def layouts_dir(self) -> str:
        return self.subdir("layouts")

    def layouts(self) -> list:
        """Named layout files in layouts/ (without the .json) — scanned fresh."""
        try:
            return sorted(f[:-5] for f in os.listdir(self.layouts_dir)
                          if f.endswith(".json"))
        except FileNotFoundError:
            return []

    def layout_path(self, name: str) -> str:
        return os.path.join(self.layouts_dir, _safe(name) + ".json")

    @property
    def working_path(self) -> str:
        """The autosaved working layout for this project (the live dashboard)."""
        return os.path.join(self.path, "working.json")

    @property
    def reports_dir(self) -> str:
        return self.subdir("reports")

    # -- creation ------------------------------------------------------------
    @classmethod
    def create(cls, path: str, name: str, description: str = "") -> "Project":
        os.makedirs(path, exist_ok=True)
        p = cls(path)
        p.meta = {
            "ferrodac_project": PROJECT_VERSION,
            "id": str(uuid.uuid4()),
            "name": name,
            "description": description,
            "created": _now(),
            "favorites": {"sources": [], "windows": []},
            "filters": {},
        }
        for d in _SUBDIRS:
            os.makedirs(os.path.join(path, d), exist_ok=True)
        p.save()
        return p


class ProjectManager:
    """Discovers projects under a root folder and tracks the active one.

    Qt-free: the active id is in-memory; the app persists/restores it (QSettings)
    and owns the root selection."""

    DEFAULT_NAME = "Default"

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._by_id: dict = {}        # id -> Project
        self._active: str = ""

    def scan(self) -> list:
        """(Re)discover projects: subfolders of root that contain a project.json."""
        found: dict = {}
        if os.path.isdir(self.root):
            for name in sorted(os.listdir(self.root)):
                d = os.path.join(self.root, name)
                if is_project(d):
                    p = Project(d)
                    if p.id:
                        found[p.id] = p
        self._by_id = found
        if self._active not in self._by_id:
            self._active = ""
        return self.projects()

    def projects(self) -> list:
        return sorted(self._by_id.values(), key=lambda p: p.name.lower())

    def get(self, pid: str):
        return self._by_id.get(pid)

    def create(self, name: str, description: str = "") -> Project:
        os.makedirs(self.root, exist_ok=True)
        base = _safe(name)
        path, n = os.path.join(self.root, base), 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(self.root, f"{base}_{n}")
        p = Project.create(path, name, description)
        self._by_id[p.id] = p
        return p

    def ensure_default(self) -> Project:
        """Guarantee at least one project exists (so the app always has a home)."""
        self.scan()
        if self._by_id:
            return self.active or self.projects()[0]
        return self.create(self.DEFAULT_NAME)

    @property
    def active(self):
        return self._by_id.get(self._active)

    def set_active(self, pid: str) -> bool:
        if pid in self._by_id:
            self._active = pid
            return True
        return False
