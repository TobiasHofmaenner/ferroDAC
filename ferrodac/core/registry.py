"""Driver registry: load device modules and collect their Device subclasses."""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from .device import Device

_BASE_DRIVER_IDS = {None, "device", "base"}


def _all_subclasses(cls) -> set[type]:
    subs = set(cls.__subclasses__())
    for s in list(subs):
        subs |= _all_subclasses(s)
    return subs


def load_package(package: ModuleType) -> None:
    for info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{info.name}")


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

    load_package(devices)
    return driver_types()
