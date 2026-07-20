"""BaseDevice's platform threading contract (DESIGN §21.1).

The audit's finding: no platform-provided serialization, so each driver
reinvented its own lock (and the Keithley forgot to). These pin the contract the
platform now guarantees — Qt-free, so they run in the fast CI gate.
"""
import threading

from ferrodac.core.base import BaseDevice
from ferrodac.core.device import Interface, Sink, SinkKind, Source


class _Dev(BaseDevice):
    driver = "threadtest"

    def __init__(self, **kw):
        super().__init__(
            "tt:1", "TT", Interface(kind="sim"),
            sources=[Source(id="ch", name="ch")],
            sinks=[Sink(id="sp", name="SP", kind=SinkKind.SETPOINT)], **kw)

    def _read(self, src):
        return 1.0, 0

    def _write(self, sink, value):
        pass


def test_write_waits_for_an_in_flight_read():
    """_read and _write are serialized per device by the platform — a write can't
    run while a read holds the device (was each driver's job before)."""
    dev = _Dev()
    reading = threading.Event()
    gate = threading.Event()
    wrote = threading.Event()
    dev._read = lambda src: (reading.set(), gate.wait(3), (1.0, 0))[-1]
    dev._write = lambda sink, value: wrote.set()

    dev.start(lambda r: None)                    # poll loop → _read blocks holding lock
    assert reading.wait(2)
    threading.Thread(target=lambda: dev.write("sp", 1.0), daemon=True).start()
    assert not wrote.wait(0.3)                    # BLOCKED behind the in-flight read
    gate.set()                                    # read releases the device
    assert wrote.wait(2)                          # …now the write proceeds
    dev.stop()


def test_serialize_io_false_opts_out():
    dev = _Dev(); dev.serialize_io = False
    reading = threading.Event()
    gate = threading.Event()
    wrote = threading.Event()
    dev._read = lambda src: (reading.set(), gate.wait(3), (1.0, 0))[-1]
    dev._write = lambda sink, value: wrote.set()
    dev.start(lambda r: None)
    assert reading.wait(2)
    dev.write("sp", 1.0)                          # NOT serialized → runs immediately
    assert wrote.wait(1)
    gate.set()
    dev.stop()


def test_throttle_rate_limits_per_key():
    dev = _Dev()
    assert dev._throttle("reconnect", 100.0) is True
    assert dev._throttle("reconnect", 100.0) is False   # within the interval
    assert dev._throttle("other", 100.0) is True        # independent key


def test_serial_drivers_share_one_port_registry():
    """Cross-driver arbitration: a port held open by any serial driver is off-limits
    to every other (they used to each hide a private _active_ports)."""
    from ferrodac.core.serial_arbiter import PORTS_IN_USE, SERIAL_LOCK
    from ferrodac.devices.keithley6221 import Keithley6221Device
    from ferrodac.devices.qms200 import QMS200Device
    from ferrodac.devices.tpg256a import TPG256ADevice
    for drv in (TPG256ADevice, QMS200Device, Keithley6221Device):
        assert drv._active_ports is PORTS_IN_USE
        assert drv._cls_lock is SERIAL_LOCK


def test_qms_control_write_is_lock_free_enqueue():
    """A filament/SEM toggle must enqueue WITHOUT blocking on the io lock the poll
    thread holds for a whole multi-second sweep — the 'toggle takes forever' fix.
    The write lands on _write_q so the poll thread can cut the sweep short and send
    it next cycle (see QMS200Device._drain_scan)."""
    import threading
    import time

    from ferrodac.devices.qms200 import QMS200Device, ProbeResult

    dev = QMS200Device(ProbeResult(port="COM_TEST", baud=19200, analyzer=0))
    holding, release = threading.Event(), threading.Event()

    def fake_sweep():                    # the poll thread holds the serial lock
        with dev._io_lock:
            holding.set()
            release.wait(2.0)

    t = threading.Thread(target=fake_sweep)
    t.start()
    assert holding.wait(1.0)
    try:
        t0 = time.monotonic()
        dev.write("filament", True)      # must NOT wait out the "sweep"
        dt = time.monotonic() - t0
    finally:
        release.set()
        t.join()
    assert dt < 0.1, f"control write blocked on the sweep lock ({dt:.2f}s)"
    assert list(dev._write_q) == [("filament", True)]
    assert dev._sink_values["filament"] is True


def test_qms_software_sink_writes_stay_off_the_wire():
    """Software-only sinks (average/smoothing/ref_pressure) apply inline and never
    enqueue a serial command — unchanged by the lock-free write override."""
    from ferrodac.devices.qms200 import QMS200Device, ProbeResult

    dev = QMS200Device(ProbeResult(port="COM_TEST", baud=19200, analyzer=0))
    dev.write("average", "8")   # ENUM sink → string option
    dev.write("ref_pressure", 1e-6)
    assert dev._avg_n == 8 and dev._ref_pressure == 1e-6
    assert len(dev._write_q) == 0        # nothing queued for the serial line
