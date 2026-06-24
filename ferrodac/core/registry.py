"""Driver registry: load device modules and collect their Device subclasses."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType

from .device import Device

log = logging.getLogger("registry")

_BASE_DRIVER_IDS = {None, "device", "base"}

# Frozen (PyInstaller one-file) builds can return nothing from iter_modules, so
# the builtin device modules are listed explicitly as a fallback.
_BUILTIN_DEVICE_MODULES = ("camera", "fake", "qms200", "shelly_cloud", "tpg256a")


def _all_subclasses(cls) -> set[type]:
    subs = set(cls.__subclasses__())
    for s in list(subs):
        subs |= _all_subclasses(s)
    return subs


def load_package(package: ModuleType, fallback=()) -> None:
    """Import every submodule of `package` so its Device subclasses register.

    Robust to a **frozen** build (PyInstaller one-file), where `iter_modules`
    can yield nothing — then the `fallback` module names are imported instead —
    and to a single module failing to import, which must never hide the rest
    (e.g. a Windows camera/QtMultimedia hiccup shouldn't also lose the sim + COM
    drivers)."""
    names = set(fallback)                              # always load the builtins
    try:
        names |= {info.name for info in pkgutil.iter_modules(package.__path__)}
    except Exception as exc:                            # can RAISE in a frozen build
        log.warning("iter_modules(%s) failed (%s) — using fallback list only",
                    package.__name__, exc)
    for name in sorted(names):
        try:
            importlib.import_module(f"{package.__name__}.{name}")
            log.info("loaded device module %r", name)
        except Exception as exc:                        # one bad driver mustn't hide the rest
            log.warning("device module %r failed to import: %s", name, exc)


def driver_types() -> list[type[Device]]:
    out: list[type[Device]] = []
    for cls in _all_subclasses(Device):
        if getattr(cls, "__abstractmethods__", None):
            continue
        if getattr(cls, "driver", None) in _BASE_DRIVER_IDS:
            continue
        out.append(cls)
    return sorted(out, key=lambda c: c.driver)


def load_builtin_drivers() -> list[type[Device]]:
    from ferrodac import devices

    load_package(devices, fallback=_BUILTIN_DEVICE_MODULES)
    return driver_types()
