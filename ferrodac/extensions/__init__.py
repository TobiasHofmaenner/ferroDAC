"""Loading external ferroDAC extensions (drivers / processors / widgets) from
plugin repos. See ``ferrodac.plugin`` for the SDK extensions code against, and
``examples/ferrodac-ext-example`` for a reference repo.
"""
from .manifest import Manifest, ManifestError, Provider, load_manifest

__all__ = ["Manifest", "ManifestError", "Provider", "load_manifest"]
