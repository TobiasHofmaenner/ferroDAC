"""The device-driver registry, focused on the FROZEN-build path.

In a PyInstaller one-file app, ``pkgutil.iter_modules(ferrodac.devices)`` yields nothing, so
``load_package`` imports ONLY ``_BUILTIN_DEVICE_MODULES``. A driver missing from that list is
silently absent from the packaged app — exactly how the LSC shipped invisible on the Windows
binary (it was missing from the fallback AND from packaging/ferrodac.spec). These tests pin
the list to the real package so it can't drift again.
"""
import pkgutil

import ferrodac.devices as devices
from ferrodac.core import registry
from ferrodac.core.registry import _BUILTIN_DEVICE_MODULES, load_builtin_drivers


def test_builtin_device_modules_cover_every_devices_submodule():
    """Every module in ferrodac/devices/ must be in the frozen-build fallback list."""
    on_disk = {info.name for info in pkgutil.iter_modules(devices.__path__)}
    missing = on_disk - set(_BUILTIN_DEVICE_MODULES)
    assert not missing, (
        f"device module(s) {sorted(missing)} are on disk but missing from "
        "_BUILTIN_DEVICE_MODULES — a frozen (PyInstaller) build would NOT import them, so "
        "their driver is invisible in the packaged app (this is the LSC-not-detected bug). "
        "Add them to the tuple in ferrodac/core/registry.py; the .spec derives from it.")


def test_frozen_build_imports_lsc_from_the_fallback(monkeypatch):
    """Simulate the frozen path (iter_modules yields nothing) and assert the LSC module is
    imported from the fallback — independent of whatever other tests already imported."""
    imported = []
    real = registry.importlib.import_module
    monkeypatch.setattr(registry.pkgutil, "iter_modules", lambda *a, **k: iter(()))
    monkeypatch.setattr(registry.importlib, "import_module",
                        lambda name: (imported.append(name), real(name))[1])
    load_builtin_drivers()
    assert "ferrodac.devices.lsc" in imported          # the regression
    assert "ferrodac.devices.lsa31" in imported        # a sibling, as a sanity anchor


def test_lsc_driver_registers():
    """End-to-end: the LSC driver class is discoverable through the registry."""
    drivers = {getattr(d, "driver", "") for d in load_builtin_drivers()}
    assert "lsc" in drivers
