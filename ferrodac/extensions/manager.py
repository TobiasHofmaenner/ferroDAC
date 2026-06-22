"""Load external extensions into a running ferroDAC.

Loading an extension = check its manifest's ``api`` version → put its directory on
``sys.path`` → import each declared ``module:Class`` entry, which **registers** it
(a processor's ``@register_processor`` / a ``Device`` subclass / a widget's
``@register_widget``). Extensions are trusted code you installed on purpose; the
trust gate (Phase 4/5) is at INSTALL time, not here.

A simple ``installed.json`` records the sources (a local repo dir today; a URL +
pinned commit once GitHub install lands) and whether each is enabled. ``load_enabled``
runs at startup, defensively — a broken extension is logged and skipped, never blocking
launch.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys

from .manifest import (API_VERSION, ManifestError, discover_extensions,
                       load_manifest)

log = logging.getLogger(__name__)


class ExtensionError(Exception):
    """An extension could not be loaded (incompatible api, bad entry, …)."""


class LoadedExtension:
    def __init__(self, manifest, errors=None):
        self.manifest = manifest
        self.errors = list(errors or [])       # per-provider import failures (strings)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def ok(self) -> bool:
        return not self.errors


def _import_entry(entry: str) -> None:
    """Import a ``module:Class`` entry. Importing the module runs its registration
    side effects; we then verify the class is actually present."""
    module, _, cls = entry.partition(":")
    mod = importlib.import_module(module)
    if cls and not hasattr(mod, cls):
        raise ExtensionError(f"{cls!r} not found in {module!r}")


class ExtensionManager:
    """Discovers, loads, and remembers installed extensions."""

    def __init__(self, root_dir: str):
        self.root = root_dir                   # where clones + installed.json live
        self._loaded: dict = {}                # name -> LoadedExtension

    # -- loading -------------------------------------------------------------
    def load_extension(self, ext_dir: str) -> LoadedExtension:
        """Load ONE extension from its directory (the one holding the manifest)."""
        return self._load_manifest(load_manifest(ext_dir))

    def load_repo(self, repo_dir: str, names=None) -> list:
        """Load all (or the named) extensions discovered in a cloned repo dir —
        monorepo-aware. Returns [LoadedExtension]."""
        out = []
        for mf in discover_extensions(repo_dir):
            if names is None or mf.name in names:
                out.append(self._load_manifest(mf))
        return out

    def _load_manifest(self, mf) -> LoadedExtension:
        if not mf.is_compatible():
            raise ExtensionError(
                f"{mf.name}: extension targets plugin api {mf.api}, this ferroDAC is "
                f"api {API_VERSION}")
        if mf.root and mf.root not in sys.path:
            sys.path.insert(0, mf.root)        # so its package becomes importable
        errors = []
        for p in mf.providers:
            try:
                _import_entry(p.entry)
            except Exception as exc:           # noqa: BLE001 — one bad provider, not fatal
                errors.append(f"{p.entry}: {exc}")
                log.warning("extension %s: provider %s failed: %s", mf.name, p.entry, exc)
        le = LoadedExtension(mf, errors)
        self._loaded[mf.name] = le
        return le

    @property
    def loaded(self) -> list:
        return list(self._loaded.values())

    # -- persistence (installed.json) ----------------------------------------
    @property
    def _records_path(self) -> str:
        return os.path.join(self.root, "installed.json")

    def records(self) -> list:
        try:
            with open(self._records_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, records: list) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self._records_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
        os.replace(tmp, self._records_path)

    def install(self, source: str, names=None, enabled: bool = True) -> list:
        """Record a source (a local repo dir today) + load it now if enabled."""
        recs = [r for r in self.records() if r.get("source") != source]
        recs.append({"source": source, "enabled": bool(enabled), "names": names})
        self._write(recs)
        return self.load_repo(source, names) if enabled else []

    def set_enabled(self, source: str, enabled: bool) -> None:
        recs = self.records()
        for r in recs:
            if r.get("source") == source:
                r["enabled"] = bool(enabled)
        self._write(recs)

    def remove(self, source: str) -> None:
        self._write([r for r in self.records() if r.get("source") != source])

    def load_enabled(self) -> None:
        """Startup hook: load every enabled recorded extension, defensively."""
        for r in self.records():
            if not r.get("enabled", True):
                continue
            src = r.get("source")
            if not src or not os.path.exists(src):
                log.warning("extension source missing, skipping: %r", src)
                continue
            try:
                self.load_repo(src, r.get("names"))
            except (ExtensionError, ManifestError) as exc:
                log.warning("extension %r failed to load: %s", src, exc)
            except Exception as exc:               # noqa: BLE001 — never block launch
                log.warning("extension %r failed to load: %s", src, exc)
