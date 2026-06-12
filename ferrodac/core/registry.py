"""Driver registry: load source modules and collect their Source subclasses.

In v1 the "library" is just Python modules in :mod:`ferrodac.sources`. Later the
same registration hook serves YAML-described drivers and user-supplied module
directories.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from .source import Source

# Base classes that are not themselves selectable drivers.
_BASE_DRIVER_IDS = {None, "source", "base"}


def _all_subclasses(cls) -> set[type]:
    subs = set(cls.__subclasses__())
    for s in list(subs):
        subs |= _all_subclasses(s)
    return subs


def load_package(package: ModuleType) -> None:
    """Import every module in a package so its Source subclasses register."""
    for info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{info.name}")


def driver_types() -> list[type[Source]]:
    """All concrete, selectable Source subclasses currently imported."""
    out: list[type[Source]] = []
    for cls in _all_subclasses(Source):
        if getattr(cls, "__abstractmethods__", None):
            continue  # still-abstract intermediates
        if getattr(cls, "driver", None) in _BASE_DRIVER_IDS:
            continue  # base/helper classes
        out.append(cls)
    return sorted(out, key=lambda c: c.driver)


def load_builtin_drivers() -> list[type[Source]]:
    """Convenience: import the built-in sources package and return its drivers."""
    from ferrodac import sources

    load_package(sources)
    return driver_types()
