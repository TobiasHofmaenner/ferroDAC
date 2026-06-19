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

    def layout_panels(self, name: str) -> int:
        """How many panels a saved layout holds — parsed so the Explorer can show
        a layout's shape, not just its name. 0 if the file is missing/unparseable."""
        try:
            with open(self.layout_path(name), encoding="utf-8") as fh:
                return len(json.load(fh).get("layout", {}).get("panels", []))
        except Exception:
            return 0

    @property
    def working_path(self) -> str:
        """The autosaved working layout for this project (the live dashboard)."""
        return os.path.join(self.path, "working.json")

    # -- curated source selection (a LENS over the catalog, not new data) ----
    @property
    def sources_path(self) -> str:
        return os.path.join(self.path, "sources.json")

    def sources(self) -> list:
        """The project's curated channels: [{key, label?, notes?}, …]. Each maps
        to a catalog source; the selection just filters the Sources view."""
        try:
            with open(self.sources_path, encoding="utf-8") as fh:
                return json.load(fh).get("sources", [])
        except Exception:
            return []

    def source_keys(self) -> set:
        return {s.get("key") if isinstance(s, dict) else s for s in self.sources()}

    def set_sources(self, sources: list) -> None:
        try:
            tmp = self.sources_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"sources": list(sources)}, fh, indent=2)
            os.replace(tmp, self.sources_path)       # atomic
        except Exception:
            pass

    @property
    def reports_dir(self) -> str:
        return self.subdir("reports")

    def recordings(self) -> list:
        """Recorded spans filed under this project — `reports/<run>/` bundles, each
        identified by its `manifest.json`. Parsed (span, #sources, #tags) so the
        Explorer can show them as cards WITHOUT re-reading the data; scanned fresh,
        newest first. The folder IS the list (no mirrored index)."""
        out = []
        reports = os.path.join(self.path, "reports")     # don't create on a read
        try:
            names = os.listdir(reports)
        except FileNotFoundError:
            return out
        for name in names:
            d = os.path.join(reports, name)
            man = os.path.join(d, "manifest.json")
            if not os.path.isfile(man):
                continue
            try:
                with open(man, encoding="utf-8") as fh:
                    m = json.load(fh)
            except Exception:
                m = {}
            out.append({
                "name": name,
                "path": d,
                "t0": m.get("t0"),
                "t1": m.get("t1"),
                "sources": len(m.get("sources", [])),
                "tags": m.get("tags", 0),
                "exported_at": m.get("exported_at", ""),
            })
        out.sort(key=lambda r: r.get("exported_at") or r["name"], reverse=True)
        return out

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
    """Tracks a REGISTRY of project folders (each may live anywhere on disk) and
    the active one. The registry — a list of folder paths + the active id — is the
    local JSON `registry_path`; the project *contents* live in those folders.

    Qt-free: the app drives the folder picker; this create-or-adopts a folder and
    records it. (Earlier 'scan one root' is gone — projects aren't confined to a
    single parent.)"""

    DEFAULT_NAME = "Default"

    def __init__(self, registry_path: str):
        self.registry_path = os.path.abspath(registry_path)
        self._by_id: dict = {}        # id -> Project
        self._active: str = ""
        self._load_registry()

    # -- registry (the tracked-folder list) ----------------------------------
    def _load_registry(self) -> None:
        paths, active = [], ""
        try:
            with open(self.registry_path, encoding="utf-8") as fh:
                reg = json.load(fh)
            paths, active = reg.get("projects", []), reg.get("active", "")
        except Exception:
            pass
        self._by_id = {}
        for p in paths:
            if is_project(p):                        # silently drop moved/deleted ones
                proj = Project(p)
                if proj.id:
                    self._by_id[proj.id] = proj
        self._active = active if active in self._by_id else ""

    def _save_registry(self) -> None:
        reg = {"projects": [p.path for p in self._by_id.values()],
               "active": self._active}
        try:
            os.makedirs(os.path.dirname(self.registry_path) or ".", exist_ok=True)
            tmp = self.registry_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(reg, fh, indent=2)
            os.replace(tmp, self.registry_path)      # atomic
        except Exception:
            pass

    reload = _load_registry                          # re-read the registry from disk

    # -- queries -------------------------------------------------------------
    def projects(self) -> list:
        return sorted(self._by_id.values(), key=lambda p: p.name.lower())

    def get(self, pid: str):
        return self._by_id.get(pid)

    @property
    def active(self):
        return self._by_id.get(self._active)

    def set_active(self, pid: str) -> bool:
        if pid in self._by_id:
            self._active = pid
            self._save_registry()
            return True
        return False

    # -- mutations -----------------------------------------------------------
    def track(self, path: str, name: str = None) -> Project:
        """ADOPT the project in `path` if it already is one, else CREATE one there.
        Registers the folder so it's tracked from now on. Returns the Project."""
        path = os.path.abspath(path)
        if is_project(path):
            p = Project(path)
        else:
            p = Project.create(path, name or os.path.basename(path.rstrip("/\\")) or "project")
        self._by_id[p.id] = p
        if not self._active:
            self._active = p.id
        self._save_registry()
        return p

    def ensure_default(self, default_dir: str, legacy_root: str = None) -> Project:
        """Guarantee at least one tracked project. Adopt any projects already in a
        legacy root (migration from the old 'scan one root' model), else create a
        built-in Default at `default_dir`."""
        if not self._by_id and legacy_root and os.path.isdir(legacy_root):
            for name in sorted(os.listdir(legacy_root)):
                d = os.path.join(legacy_root, name)
                if is_project(d):
                    self.track(d)
        if not self._by_id:
            self.track(default_dir, self.DEFAULT_NAME)
        if not self._active:
            self.set_active(self.projects()[0].id)
        return self.active
