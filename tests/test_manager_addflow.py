"""add_user_device / on_forget — the MINTED (non-discovered) device add flow that
the Python-source feature rides on (manager-addflow unit).

All headless: a session QApplication only, no MainWindow. Pins:
  * add_user_device injects a minted device STRAIGHT into _active (it never passes
    through _available, which the discovery worker owns and would purge);
  * it onboards a data-plane uuid, and with user=True emits device_added (the
    curate trigger); user=False (a startup restore) stays silent;
  * the worker then connects + starts streaming on the engine;
  * remove() calls the device's on_forget() cleanup hook (so a removed source's
    saved def can't come back on restart) and is safe on drivers that lack it.
"""
import time

from ferrodac.core.base import BaseDevice
from ferrodac.core.device import Interface, Source
from ferrodac.core.engine import Engine
from ferrodac.core.manager import DeviceManager


class _MintedDevice(BaseDevice):
    """A stand-in for PythonSourceDevice: discoverable=False, minted by hand and
    handed to add_user_device rather than found by a scan."""

    driver = "test_minted"
    discoverable = False

    def __init__(self, iid="pysrc-1", name="Minted"):
        super().__init__(iid, name, Interface("sim"), sources=[Source("v", "Value")])
        self.connected = False
        self.forgot = False

    @classmethod
    def discover(cls):          # never discovered — minted only
        return []

    def _connect(self):
        self.connected = True

    def _read(self, source):
        return 1.0, 0

    def on_forget(self):        # the manager's remove() cleanup hook
        self.forgot = True


class _PlainDevice(_MintedDevice):
    """A driver WITHOUT on_forget — remove() must not choke on its absence."""
    on_forget = None            # shadow the hook so getattr(..., "on_forget") is None


def _spin(app, cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    app.processEvents()
    return cond()


def test_add_user_device_injects_active_and_curates(qapp):
    engine = Engine()
    mgr = DeviceManager([], engine=engine, registry=None)
    added = []
    mgr.device_added.connect(added.append)

    dev = _MintedDevice()
    mgr.add_user_device(dev, user=True)

    iids = [d.instance_id for d in mgr.active_descriptors()]
    assert dev.instance_id in iids                  # straight into _active…
    assert dev.instance_id not in mgr._available    # …never parked in _available
    assert dev.uuid is not None                     # onboarded a data-plane uuid
    assert added == [dev.instance_id]               # user add → curate trigger

    # the worker connected the device and handed it to the engine to stream
    assert _spin(qapp, lambda: dev.connected and dev.instance_id in engine._devices)
    engine.shutdown()


def test_restore_is_silent(qapp):
    mgr = DeviceManager([], engine=None, registry=None)
    added = []
    mgr.device_added.connect(added.append)

    mgr.add_user_device(_MintedDevice("pysrc-2"), user=False)   # a restore
    assert added == []                              # never auto-curates on restore
    assert "pysrc-2" in [d.instance_id for d in mgr.active_descriptors()]


def test_add_user_device_is_idempotent(qapp):
    mgr = DeviceManager([], engine=None, registry=None)
    dev = _MintedDevice("pysrc-4")
    mgr.add_user_device(dev, user=True)
    mgr.add_user_device(dev, user=True)             # second add is a no-op
    iids = [d.instance_id for d in mgr.active_descriptors()]
    assert iids.count("pysrc-4") == 1


def test_remove_calls_on_forget(qapp):
    engine = Engine()
    mgr = DeviceManager([], engine=engine, registry=None)
    dev = _MintedDevice("pysrc-3")
    mgr.add_user_device(dev, user=True)
    assert _spin(qapp, lambda: dev.connected)

    mgr.remove(dev.instance_id)
    assert dev.forgot is True                       # saved def dropped on explicit remove
    assert "pysrc-3" not in [d.instance_id for d in mgr.active_descriptors()]
    engine.shutdown()


def test_remove_without_hook_is_safe(qapp):
    engine = Engine()
    mgr = DeviceManager([], engine=engine, registry=None)
    dev = _PlainDevice("plain-1")
    mgr.add_user_device(dev, user=True)
    assert _spin(qapp, lambda: dev.connected)

    mgr.remove(dev.instance_id)                     # no on_forget → must not raise
    assert "plain-1" not in [d.instance_id for d in mgr.active_descriptors()]
    engine.shutdown()
