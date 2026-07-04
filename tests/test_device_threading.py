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
