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
import re
import shutil
import subprocess
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


def _repo_name(url: str) -> str:
    # Last path component of a URL OR a local path — split on BOTH separators so a
    # Windows local path (C:\…\repo, backslashes) doesn't become the whole encoded path
    # as the clone-dir name (which blows past Windows' MAX_PATH on a deep source dir).
    base = re.split(r"[/\\]", url.rstrip("/\\"))[-1]
    if base.endswith(".git"):
        base = base[:-4]
    return re.sub(r"[^\w.-]", "_", base) or "extension"


def _import_entry(entry: str):
    """Import a ``module:Class`` entry and return the class. Importing the module runs
    its registration side effects; we then verify the class is actually present."""
    module, _, cls = entry.partition(":")
    mod = importlib.import_module(module)
    if not cls:
        return None
    if not hasattr(mod, cls):
        raise ExtensionError(f"{cls!r} not found in {module!r}")
    return getattr(mod, cls)


def entry_file(root: str, entry: str):
    """The source file for a ``module:Class`` entry under a repo root, WITHOUT importing
    it (so 'Show source' never executes a disabled extension). None if not found."""
    rel = entry.split(":")[0].replace(".", os.sep)
    for cand in (os.path.join(root, rel + ".py"), os.path.join(root, rel, "__init__.py")):
        if os.path.exists(cand):
            return cand
    return None


class ExtensionManager:
    """Discovers, loads, and remembers installed extensions."""

    def __init__(self, root_dir: str):
        self.root = root_dir                   # where clones + installed.json live
        self._loaded: dict = {}                # name -> LoadedExtension
        self._index: dict = {}                 # kind/driver -> (Manifest, Provider, class)

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
                obj = _import_entry(p.entry)
                kind = getattr(obj, "kind", None) or getattr(obj, "driver", None)
                if kind:
                    self._index[kind] = (mf, p, obj)
            except Exception as exc:           # noqa: BLE001 — one bad provider, not fatal
                errors.append(f"{p.entry}: {exc}")
                log.warning("extension %s: provider %s failed: %s", mf.name, p.entry, exc)
        le = LoadedExtension(mf, errors)
        self._loaded[mf.name] = le
        return le

    @property
    def loaded(self) -> list:
        return list(self._loaded.values())

    # -- provenance lookups (by a provider's kind/driver) --------------------
    def provider_for(self, kind: str):
        """(Manifest, Provider, class) for a loaded provider, or None."""
        return self._index.get(kind)

    def source_for(self, kind: str) -> str:
        """The provider's source — read from its file (no import/execution)."""
        rec = self._index.get(kind)
        if rec is None:
            return ""
        mf, p, _ = rec
        path = entry_file(mf.root, p.entry)
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                pass
        return ""

    def whitepaper_for(self, kind: str):
        """Absolute path to the provider's white paper, or None."""
        rec = self._index.get(kind)
        return rec[0].whitepaper_path(rec[1]) if rec else None

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

    def _record(self, source, clone, commit, names, enabled) -> None:
        recs = [r for r in self.records() if r.get("source") != source]
        recs.append({"source": source, "clone": clone, "commit": commit,
                     "enabled": bool(enabled), "names": names})
        self._write(recs)

    def install(self, source: str, names=None, enabled: bool = True) -> list:
        """Record a LOCAL extension dir + load it now if enabled (no git). For a git
        repo URL, use install_url (clone + pin)."""
        self._record(source, None, None, names, enabled)
        return self.load_repo(source, names) if enabled else []

    # -- git install (clone + pin) -------------------------------------------
    def _git(self, args, cwd=None) -> str:
        try:
            return subprocess.run(["git", *args], cwd=cwd, check=True,
                                  capture_output=True, text=True).stdout
        except FileNotFoundError as exc:
            raise ExtensionError("git is not installed") from exc
        except subprocess.CalledProcessError as exc:
            raise ExtensionError(
                f"git {' '.join(args)} failed: {(exc.stderr or '').strip()}") from exc

    def clone(self, url: str, ref: str = None):
        """Clone (or fetch+update) a repo into the extensions dir and check out a
        pinned ref. Returns (clone_dir, resolved_commit_sha)."""
        dest = os.path.join(self.root, "clones", _repo_name(url))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isdir(os.path.join(dest, ".git")):
            self._git(["fetch", "--all", "--tags", "--prune"], cwd=dest)
        else:
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            self._git(["clone", url, dest])
        if ref:
            self._git(["checkout", ref], cwd=dest)
        sha = self._git(["rev-parse", "HEAD"], cwd=dest).strip()
        return dest, sha

    def prepare(self, url: str, ref: str = None):
        """Clone/update + discover, WITHOUT recording or loading — for the trust gate.
        Returns (clone_dir, commit_sha, [Manifest])."""
        dest, sha = self.clone(url, ref)
        return dest, sha, discover_extensions(dest)

    def install_url(self, url: str, ref: str = None, names=None, enabled: bool = True):
        """Clone+pin a git repo, record it (source=url, with the resolved commit), and
        load the (named) extensions. Returns ([LoadedExtension], clone_dir, sha)."""
        dest, sha = self.clone(url, ref)
        self._record(url, dest, sha, names, enabled)
        return (self.load_repo(dest, names) if enabled else []), dest, sha

    def update(self, source: str, ref: str = None) -> str:
        """Re-pin a git-sourced extension to a new commit/ref; returns the new sha."""
        recs = self.records()
        rec = next((r for r in recs if r.get("source") == source), None)
        if rec is None:
            raise ExtensionError(f"not installed: {source}")
        dest, sha = self.clone(source, ref)
        rec["clone"], rec["commit"] = dest, sha
        self._write(recs)
        return sha

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
            d = r.get("clone") or r.get("source")     # git sources load from the clone
            if not d or not os.path.exists(d):
                log.warning("extension source missing, skipping: %r", r.get("source"))
                continue
            try:
                self.load_repo(d, r.get("names"))
            except (ExtensionError, ManifestError) as exc:
                log.warning("extension %r failed to load: %s", r.get("source"), exc)
            except Exception as exc:               # noqa: BLE001 — never block launch
                log.warning("extension %r failed to load: %s", r.get("source"), exc)
