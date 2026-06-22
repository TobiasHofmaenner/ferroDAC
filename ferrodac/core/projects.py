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

    is_hub = False                    # a LOCAL folder project (vs HubProject)

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
        return self.file_path("working.json")

    # -- file boundary -------------------------------------------------------
    # The SINGLE place a project-relative path is resolved to a concrete location.
    # Everything that touches a project file should go through this (or the dir
    # accessors), never `project.path` directly — so a future backend (e.g. a git
    # working tree living elsewhere, DESIGN §8.2) swaps HERE, not across the app.
    def file_path(self, relpath: str) -> str:
        return os.path.join(self.path, relpath)

    @property
    def readme_path(self) -> str:
        return self.file_path("README.md")

    def ensure_readme(self) -> str | None:
        """The README path, writing a starter if it's missing. Returns the path, or
        None if it couldn't be created."""
        path = self.readme_path
        if not os.path.exists(path):
            try:
                os.makedirs(self.path, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(f"# {self.name}\n\n_Describe this project — what, why, "
                             "and what you expect to see._\n")
            except Exception:                        # noqa: BLE001
                pass
        return path if os.path.exists(path) else None

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

    # -- docs (reference files dropped in docs/) -----------------------------
    @property
    def docs_dir(self) -> str:
        return self.subdir("docs")

    def docs(self) -> list:
        """Reference files in docs/ (datasheets, notes, protocols, plots) — shown
        as cards so you can reopen them. Scanned fresh; the folder IS the list."""
        out = []
        docs = os.path.join(self.path, "docs")           # don't create on a read
        try:
            names = sorted(os.listdir(docs))
        except FileNotFoundError:
            return out
        for name in names:
            p = os.path.join(docs, name)
            if os.path.isfile(p):
                out.append({"name": name, "path": p,
                            "ext": os.path.splitext(name)[1].lstrip(".").lower()})
        return out

    def import_doc(self, src: str) -> str:
        """Copy an external file into docs/ (a reference attachment); returns the
        destination path. The folder stays the source of truth."""
        import shutil
        dest = os.path.join(self.docs_dir, os.path.basename(src))
        shutil.copy2(src, dest)
        return dest

    # -- favourites: saved time-windows (bookmarks) — a nav aid, in the meta --
    def windows(self) -> list:
        """Saved time-windows: [{name, t0, t1}, …]. A bookmark for an interesting
        span so you can jump back to it without re-finding it on the timeline."""
        return list((self.meta.get("favorites") or {}).get("windows") or [])

    def add_window(self, name: str, t0: float, t1: float) -> None:
        fav = self.meta.setdefault("favorites", {})
        wins = [w for w in (fav.get("windows") or []) if w.get("name") != name]
        wins.append({"name": name, "t0": float(t0), "t1": float(t1)})
        fav["windows"] = wins
        self.save()

    def remove_window(self, name: str) -> None:
        fav = self.meta.setdefault("favorites", {})
        fav["windows"] = [w for w in (fav.get("windows") or []) if w.get("name") != name]
        self.save()

    @property
    def version(self) -> int:
        return int(self.meta.get("version", 1))

    # -- portable record (folder <-> wire, for hub sync) ---------------------
    # A plain proto-shaped dict of the SHAREABLE content (meta + lens + bookmarks
    # + named-layout blobs). Proto-free on purpose: the hub/client convert
    # dict<->pb.Project, but the FOLDER stays the source of truth either side — so
    # a hub project is the same mountable layout as a local one (DESIGN §8.1).
    def to_record(self) -> dict:
        layouts = {}
        for name in self.layouts():
            try:
                with open(self.layout_path(name), encoding="utf-8") as fh:
                    layouts[name] = fh.read()
            except Exception:                        # noqa: BLE001  unreadable → skip
                pass
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created": self.meta.get("created", ""),
            "modified": self.meta.get("modified", ""),
            "origin_id": self.meta.get("origin_id", ""),
            "version": self.version,
            "deleted": False,
            "sources": list(self.source_keys()),
            "windows": [{"name": w.get("name", ""),
                         "t0": float(w.get("t0") or 0.0),
                         "t1": float(w.get("t1") or 0.0)} for w in self.windows()],
            "layouts": layouts,
        }

    def apply_record(self, rec: dict) -> None:
        """Materialise a record into THIS folder (creating it) — meta, the channel
        lens, bookmarks and the named-layout files. Used by the hub to write a
        published project as a real, mountable project folder."""
        os.makedirs(self.path, exist_ok=True)
        self.meta.setdefault("ferrodac_project", PROJECT_VERSION)
        self.meta["id"] = rec.get("id") or self.meta.get("id") or str(uuid.uuid4())
        self.meta["name"] = rec.get("name") or self.meta.get("name") or "project"
        self.meta["description"] = rec.get("description", "")
        self.meta["origin_id"] = rec.get("origin_id", "")
        self.meta["version"] = int(rec.get("version", 1))
        if rec.get("created"):
            self.meta["created"] = rec["created"]
        self.meta.setdefault("favorites", {})["windows"] = [
            {"name": w.get("name", ""), "t0": w.get("t0"), "t1": w.get("t1")}
            for w in (rec.get("windows") or [])]
        self.set_sources([{"key": k} for k in (rec.get("sources") or [])])
        self.save()                                  # project.json (meta)
        # named layouts → layouts/*.json; drop any the record no longer carries
        wanted = rec.get("layouts") or {}
        keep = {_safe(n) for n in wanted}
        for stale in self.layouts():
            if _safe(stale) not in keep:
                try:
                    os.remove(self.layout_path(stale))
                except OSError:
                    pass
        for name, blob in wanted.items():
            try:
                with open(self.layout_path(name), "w", encoding="utf-8") as fh:
                    fh.write(blob)
            except Exception:                        # noqa: BLE001
                pass

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


class HubProject(Project):
    """A project that lives on the HUB. The folder here is a local CACHE — a
    one-way render of the hub's authoritative record (written by apply_record;
    the hub wins). Edits publish a new RECORD (LWW by version); we never two-way
    sync the folder. `is_hub` lets the UI badge it and the app republish on change.

    `bump()` raises the version and returns the record to publish — the app calls
    it after a local edit (curate/bookmark/layout) to push the change up."""

    is_hub = True

    def bump(self) -> dict:
        self.meta["version"] = self.version + 1
        self.save()
        return self.to_record()


class ProjectManager:
    """Tracks a REGISTRY of project folders (each may live anywhere on disk) and
    the active one. The registry — a list of folder paths + the active id — is the
    local JSON `registry_path`; the project *contents* live in those folders.

    Qt-free: the app drives the folder picker; this create-or-adopts a folder and
    records it. (Earlier 'scan one root' is gone — projects aren't confined to a
    single parent.)"""

    DEFAULT_NAME = "Default"

    def __init__(self, registry_path: str, hub_cache_dir: str = None):
        self.registry_path = os.path.abspath(registry_path)
        self._by_id: dict = {}        # id -> Project (LOCAL folder projects)
        self._hub_by_id: dict = {}    # id -> HubProject (synced from the hub, cached)
        self._hub_cache = os.path.abspath(hub_cache_dir) if hub_cache_dir else \
            os.path.join(os.path.dirname(self.registry_path) or ".", "hub_cache")
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

    # -- queries (LOCAL + HUB merged) ----------------------------------------
    def projects(self) -> list:
        return sorted(list(self._by_id.values()) + list(self._hub_by_id.values()),
                      key=lambda p: p.name.lower())

    def get(self, pid: str):
        return self._by_id.get(pid) or self._hub_by_id.get(pid)

    @property
    def active(self):
        return self.get(self._active)

    def set_active(self, pid: str) -> bool:
        if pid in self._by_id or pid in self._hub_by_id:
            self._active = pid
            self._save_registry()                    # hub ids are persisted too, but
            return True                              # resolve to local on a cold start
        return False

    # -- hub projects (synced records, cached as folders; the hub is truth) --
    def apply_hub_record(self, rec: dict):
        """Materialise/update a hub project from a wire record (LWW by version),
        rendered into the local cache folder. Returns the HubProject, or None if it
        was a tombstone (dropped). The caller refreshes the UI."""
        pid = rec.get("id")
        if not pid:
            return None
        if rec.get("deleted"):
            self.drop_hub(pid)
            return None
        cur = self._hub_by_id.get(pid)
        if cur is not None and int(rec.get("version") or 1) < cur.version:
            return cur                               # stale — keep what we have
        p = cur or HubProject(os.path.join(self._hub_cache, pid))
        p.apply_record(rec)
        self._hub_by_id[pid] = p
        return p

    def drop_hub(self, pid: str) -> None:
        self._hub_by_id.pop(pid, None)
        if self._active == pid:
            self._active = ""                        # caller picks a local fallback

    def clear_hub(self) -> None:
        """Forget all hub projects (e.g. on disconnect — they're not offline). Fall
        back to a local project if the active one was on the hub."""
        self._hub_by_id.clear()
        if self.active is None and self._by_id:
            self.set_active(self.projects()[0].id)

    def share_to_hub(self, pid: str) -> dict:
        """Promote a LOCAL project to a hub project: returns the record to publish.
        The local folder stays; once the hub echoes it back it appears as a hub
        project too (same id), so the caller typically drops the local copy after."""
        p = self._by_id.get(pid)
        if p is None:
            return {}
        p.meta.setdefault("origin_id", "")
        return p.to_record()

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

    def untrack(self, pid: str) -> None:
        """Stop tracking a LOCAL project (its folder stays on disk). Used by
        share-to-hub, which MOVES a project to the hub (its hub copy, same id,
        takes over; the local folder remains as an offline backup)."""
        if pid in self._by_id:
            self._by_id.pop(pid, None)
            self._save_registry()

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
