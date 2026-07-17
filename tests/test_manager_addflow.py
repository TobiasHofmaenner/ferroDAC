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
    """A stand-in for PythonDevice: discoverable=False, minted by hand and
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


def test_device_tag_channel_wired_on_add(qapp):
    """A device added to the manager gets a tag channel (DESIGN §7.3): its emit_tag
    reaches DeviceManager.device_tag. Before it is wired, emit_tag is a safe no-op."""
    mgr = DeviceManager([], engine=None, registry=None)
    got = []
    mgr.device_tag.connect(got.append)

    dev = _MintedDevice("pysrc-tag")
    dev.emit_tag(object())                           # no sink yet → safe no-op, no crash
    assert got == []

    mgr.add_user_device(dev, user=True)
    assert dev._tag_sink is not None                 # manager injected the device→tag sink
    sentinel = object()
    dev.emit_tag(sentinel)                           # device raises a tag…
    qapp.processEvents()
    assert got == [sentinel]                          # …it reached the manager's device_tag


def test_device_prompt_channel_wired_on_add(qapp):
    """A device added to the manager gets a request channel (core.interaction): its ask()
    reaches DeviceManager.device_prompt, carrying BOTH the Prompt and the on_response
    callback. Before it is wired, ask() is a safe no-op — the tag channel's twin."""
    from ferrodac.core.interaction import Prompt

    mgr = DeviceManager([], engine=None, registry=None)
    got = []
    mgr.device_prompt.connect(lambda pr, cb: got.append((pr, cb)))

    dev = _MintedDevice("pysrc-prompt")
    dev.ask(Prompt("d", "?"), lambda a: None)        # no sink yet → safe no-op, no crash
    assert got == []

    mgr.add_user_device(dev, user=True)
    assert dev._prompt_sink is not None              # manager injected the device→prompt sink
    prompt = Prompt(dev.data_id, "Retract the arm?")
    cb = lambda a: None                              # noqa: E731 — the driver's response cb
    dev.ask(prompt, cb)                              # device raises a request…
    qapp.processEvents()
    assert got == [(prompt, cb)]                     # …it reached device_prompt with its callback


def test_device_prompt_withdraw_channel_wired_on_add(qapp):
    """A device also gets a WITHDRAW channel: withdraw_prompt(id) reaches
    DeviceManager.device_prompt_withdrawn — the device resolved its own prompt (?DONE) and the
    app retires it from the inbox. Safe no-op before it is wired (the prompt channel's twin)."""
    mgr = DeviceManager([], engine=None, registry=None)
    got = []
    mgr.device_prompt_withdrawn.connect(got.append)

    dev = _MintedDevice("pysrc-wd")
    dev.withdraw_prompt("x")                          # no sink yet → safe no-op, no crash
    assert got == []

    mgr.add_user_device(dev, user=True)
    assert dev._prompt_withdraw_sink is not None      # manager injected the withdraw sink
    dev.withdraw_prompt("prompt-uuid-1")
    qapp.processEvents()
    assert got == ["prompt-uuid-1"]                   # …it reached device_prompt_withdrawn


def test_remove_emits_device_removed_for_prompt_withdrawal(qapp):
    """remove() announces the device's ids on device_removed so the app can withdraw its
    open requests (core.interaction) — carries (uuid, instance_id)."""
    engine = Engine()
    mgr = DeviceManager([], engine=engine, registry=None)
    dev = _MintedDevice("pysrc-rm")
    mgr.add_user_device(dev, user=True)
    assert _spin(qapp, lambda: dev.connected)

    seen = []
    mgr.device_removed.connect(seen.append)
    mgr.remove(dev.instance_id)
    assert seen == [(getattr(dev, "uuid", None), "pysrc-rm")]
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
