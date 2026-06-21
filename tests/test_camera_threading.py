"""Camera discovery must not touch Qt Multimedia off the GUI thread.

`QMediaDevices.videoInputs()` initialises the platform multimedia backend on the
CALLING thread; the DeviceManager runs discovery on a worker, so calling it there
brought Qt Multimedia up off the GUI thread — the cross-thread Qt bug the
diagnostics harness flagged. The fix: enumerate on the GUI thread
(``prepare_discovery`` → ``install_camera_enumeration``) into a cache that
``discover()`` only reads.
"""

import pytest

pytest.importorskip("qtpy")
# Qt Multimedia can fail to LOAD (not merely be absent) on a headless CI runner —
# e.g. `libpulse.so.0: cannot open shared object file`. importorskip re-raises that
# (the failing module is PySide6.QtMultimedia, not the name we asked for), so guard
# the import ourselves and skip the whole module. Camera enumeration is GUI-only.
try:
    import qtpy.QtMultimedia  # noqa: F401
except Exception as exc:       # noqa: BLE001
    pytest.skip(f"Qt Multimedia unavailable: {exc}", allow_module_level=True)


def test_discover_reads_cache_not_backend(monkeypatch):
    from ferrodac.devices import camera

    class _Boom:
        @staticmethod
        def videoInputs():
            raise AssertionError(
                "discover() must not enumerate Qt Multimedia (off-thread backend init)")

    monkeypatch.setattr(camera, "QMediaDevices", _Boom)
    monkeypatch.setattr(camera, "_video_inputs", [])
    assert camera.CameraDevice.discover() == []          # reads the cache, never the backend
    assert hasattr(camera.CameraDevice, "prepare_discovery")


@pytest.mark.ui
def test_prepare_discovery_installs_gui_thread_enumeration(qapp):
    from ferrodac.devices import camera

    camera._devices_watcher = None                       # reset idempotency for the test
    camera.CameraDevice.prepare_discovery()
    assert camera._devices_watcher is not None           # a GUI-thread QMediaDevices owns it
    camera.CameraDevice.discover()                        # reads the enumerated cache, no raise
