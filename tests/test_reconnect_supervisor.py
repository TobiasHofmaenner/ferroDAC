"""The BaseDevice reconnect supervisor (DESIGN §21.1, issue #10) — opt-in auto-reconnect of
a dropped link, keyed on the driver's transport-failure verdict (never on a value)."""

from ferrodac.core.base import BaseDevice
from ferrodac.core.device import Interface, Source, Status


class _Recon(BaseDevice):
    reconnectable = True
    reconnect_backoff = 0.0                     # no throttle wait in tests

    def __init__(self):
        super().__init__("recon-1", "Recon", Interface("sim"), sources=[Source("v", "V")])
        self.healthy = True
        self.present = True
        self.connects = 0
        self.disconnects = 0

    def _read(self, s):
        return 1.0, 0

    def _connect(self):
        self.connects += 1

    def _disconnect(self):
        self.disconnects += 1

    def _link_healthy(self):
        return self.healthy

    def _port_present(self):
        return self.present


class _NonRecon(_Recon):
    reconnectable = False                       # the default — the supervisor must ignore it


def test_supervisor_trips_disconnects_then_reconnects_on_recovery():
    d = _Recon()
    d._status = Status.CONNECTED
    d._supervise()                              # healthy → nothing happens
    assert d._status == Status.CONNECTED and d.disconnects == 0

    d.healthy = False                           # the link drops
    d._supervise()
    assert d._status == Status.ERROR and d.disconnects == 1   # → ERROR + low-level _disconnect

    d.healthy = True                            # the link recovers…
    d._supervise()                              # …the reconnect phase reopens it
    assert d._status == Status.CONNECTED and d.connects == 1
    d._supervise()                              # stays healthy, no churn
    assert d._status == Status.CONNECTED and d.connects == 1


def test_supervisor_ignores_non_reconnectable_devices():
    d = _NonRecon()
    d._status = Status.CONNECTED
    d.healthy = False                           # even a reported-dead link…
    d._supervise()
    assert d._status == Status.CONNECTED and d.disconnects == 0   # …is ignored (opt-in gate)
    d._status = Status.ERROR
    d._supervise()
    assert d.connects == 0                       # never reconnects a non-reconnectable device


def test_supervisor_does_not_reopen_a_removed_port():
    d = _Recon()
    d._status = Status.ERROR
    d.present = False                           # the port is physically gone
    for _ in range(5):
        d._supervise()
    assert d.connects == 0 and d._status == Status.ERROR   # no busy-reopen of a vanished port


def test_reconnect_failure_stays_error_and_retries():
    d = _Recon()

    def _boom():
        d.connects += 1
        raise RuntimeError("still down")
    d._connect = _boom
    d._status = Status.ERROR
    d._supervise()
    assert d._status == Status.ERROR and d.connects == 1   # stayed ERROR
    d._supervise()
    assert d.connects == 2                                  # …and retried (backoff=0)


def test_link_healthy_default_is_no_opinion():
    d = _NonRecon()                             # a plain device gives no verdict → never reconnected
    assert BaseDevice._link_healthy(d) is None
    assert BaseDevice._port_present(d) is True
