"""Parse + validate a plugin repo's ``ferrodac-extension.toml`` manifest.

The manifest declares metadata + the explicit providers (each a ``module:Class``
entry, optionally with a white paper). Parsing is stdlib-only (``tomllib``); it never
imports the providers — that's the loader's job, behind the trust gate.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from ..plugin import API_VERSION

MANIFEST_NAME = "ferrodac-extension.toml"
_ROLES = (("processor", "processors"), ("driver", "drivers"), ("widget", "widgets"))


class ManifestError(ValueError):
    """A malformed or incompatible extension manifest."""


@dataclass
class Provider:
    role: str                      # "processor" | "driver" | "widget"
    entry: str                     # "package.module:ClassName"
    whitepaper: str | None = None  # repo-relative path to a PDF/MD, if any


@dataclass
class Manifest:
    name: str
    version: str
    api: int
    description: str = ""
    authors: list = field(default_factory=list)
    license: str = ""
    homepage: str = ""
    providers: list = field(default_factory=list)   # [Provider]
    root: str = ""                                   # the repo dir the manifest came from

    def of_role(self, role: str) -> list:
        return [p for p in self.providers if p.role == role]

    @property
    def processors(self) -> list: return self.of_role("processor")

    @property
    def drivers(self) -> list: return self.of_role("driver")

    @property
    def widgets(self) -> list: return self.of_role("widget")

    def is_compatible(self, api: int = API_VERSION) -> bool:
        """Exact-match the plugin-API version for now (a strict, honest gate)."""
        return self.api == api

    def whitepaper_path(self, provider: Provider) -> str | None:
        """Absolute path to a provider's white paper, if it exists on disk."""
        if not provider.whitepaper or not self.root:
            return None
        p = os.path.join(self.root, provider.whitepaper)
        return p if os.path.exists(p) else None


def load_manifest(path: str) -> Manifest:
    """Load a manifest from a repo directory (containing ferrodac-extension.toml) or
    the toml file directly. Raises ManifestError on anything malformed."""
    root = path
    if os.path.isdir(path):
        toml_path = os.path.join(path, MANIFEST_NAME)
    else:
        toml_path = path
        root = os.path.dirname(path)
    if not os.path.exists(toml_path):
        raise ManifestError(f"no {MANIFEST_NAME} found at {path!r}")
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ManifestError(f"could not read {toml_path!r}: {exc}") from exc

    ext = data.get("extension")
    if not isinstance(ext, dict):
        raise ManifestError("missing [extension] table")
    if not ext.get("name"):
        raise ManifestError("[extension] needs a name")
    if "api" not in ext:
        raise ManifestError("[extension] needs an api version")
    try:
        api = int(ext["api"])
    except (TypeError, ValueError) as exc:
        raise ManifestError("[extension] api must be an integer") from exc

    providers = []
    for role, key in _ROLES:
        for e in data.get(key, []) or []:
            if not isinstance(e, dict) or not e.get("entry"):
                raise ManifestError(f"each [[{key}]] needs an entry = \"module:Class\"")
            if ":" not in e["entry"]:
                raise ManifestError(f"{key} entry {e['entry']!r} must be 'module:Class'")
            providers.append(Provider(role, e["entry"], e.get("whitepaper")))

    return Manifest(
        name=ext["name"], version=str(ext.get("version", "")), api=api,
        description=str(ext.get("description", "")),
        authors=list(ext.get("authors", [])), license=str(ext.get("license", "")),
        homepage=str(ext.get("homepage", "")), providers=providers, root=root)
