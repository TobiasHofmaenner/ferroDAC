"""Loading external ferroDAC extensions (drivers / processors / widgets) from
plugin repos. See ``ferrodac.plugin`` for the SDK extensions code against, and
``examples/ferrodac-ext-example`` for a reference repo.
"""
from .manager import ExtensionError, ExtensionManager, LoadedExtension
from .manifest import (Manifest, ManifestError, Provider, discover_extensions,
                       load_manifest)

# The official, first-party extensions monorepo — offered as a one-click default in the
# Extensions manager (still installed only on the user's explicit opt-in, via the gate).
OFFICIAL_EXTENSIONS_URL = "https://github.com/TobiasHofmaenner/ferrodac-extensions"

__all__ = ["Manifest", "ManifestError", "Provider", "load_manifest",
           "discover_extensions", "ExtensionManager", "ExtensionError",
           "LoadedExtension", "OFFICIAL_EXTENSIONS_URL"]
