"""Loading external ferroDAC extensions (drivers / processors / widgets) from
plugin repos. See ``ferrodac.plugin`` for the SDK extensions code against, and
``examples/ferrodac-ext-example`` for a reference repo.
"""
from .manager import ExtensionError, ExtensionManager, LoadedExtension
from .manifest import (Manifest, ManifestError, Provider, discover_extensions,
                       load_manifest)

__all__ = ["Manifest", "ManifestError", "Provider", "load_manifest",
           "discover_extensions", "ExtensionManager", "ExtensionError",
           "LoadedExtension"]
