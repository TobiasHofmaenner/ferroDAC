"""The shared serial open+identify handshake helper (ferrodac.core.serial_connect, issue #9).

Unit-tested against a tiny duck-typed `conn` (no pyserial, no driver) that records its op
order, so the warm-up + retry contract is pinned independently of LSA31/LSC."""

import pytest

from ferrodac.core import serial_connect
from ferrodac.core.serial_connect import TERM, open_and_identify, posix_exclusive_kwargs


class _Ser:
    def __init__(self):
        self.ops = []

    def write(self, d):
        self.ops.append(("write", bytes(d)))

    def flush(self):
        self.ops.append(("flush",))

    def reset_input_buffer(self):
        self.ops.append(("reset",))


class _Conn:
    """A minimal LSA31/LSC-shaped controller: open()/idn()/close()/_ser."""

    def __init__(self, idn_seq, open_raises=None):
        self._idn_seq = list(idn_seq)         # per-idn() call: a raw reply string OR an Exception
        self._ser = None
        self._ser_obj = _Ser()
        self.open_raises = open_raises
        self.closed = False

    def open(self):
        if self.open_raises is not None:
            raise self.open_raises
        self._ser = self._ser_obj
        self._ser.ops.append(("open",))
        return self

    def idn(self):
        self._ser.ops.append(("idn",))
        r = self._idn_seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


def _validate(raw):                           # a stand-in for the driver's _parse_idn
    return ("SN", "FW") if raw == "OK" else None


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    monkeypatch.setattr(serial_connect.time, "sleep", lambda s: None)


def test_warmup_flushes_parser_then_identifies():
    c = _Conn(["OK"])
    assert open_and_identify(c, _validate) == ("SN", "FW")
    ops = [o[0] for o in c._ser_obj.ops]
    assert ops[0] == "open"
    assert ("write", TERM) in c._ser_obj.ops          # a lone terminator flushed the parser…
    i_write = next(i for i, o in enumerate(c._ser_obj.ops) if o == ("write", TERM))
    i_idn = ops.index("idn")
    assert i_write < i_idn                             # …before the first *IDN?
    assert "reset" in ops[i_write:i_idn]              # and the host buffer was reset before it


def test_first_idn_junk_then_clean_on_retry():
    c = _Conn([Exception('ERR,1,"unknown command"'), "OK"])   # 1st sacrificial, 2nd clean
    assert open_and_identify(c, _validate) == ("SN", "FW")
    assert [o for o in c._ser_obj.ops].count(("idn",)) == 2


def test_all_attempts_fail_returns_none_and_caller_owns_close():
    c = _Conn([Exception("e"), Exception("e"), Exception("e")])
    assert open_and_identify(c, _validate, attempts=3) is None
    assert c.closed is False                           # helper never closes — the caller does
    assert [o for o in c._ser_obj.ops].count(("idn",)) == 3


def test_a_clean_but_wrong_device_line_also_retries_then_none():
    c = _Conn(["Keithley,2000", "Keithley,2000", "Keithley,2000"])   # valid line, wrong vendor
    assert open_and_identify(c, _validate, attempts=3) is None


def test_open_failure_propagates_never_masked_as_none():
    c = _Conn(["OK"], open_raises=OSError("port busy"))
    with pytest.raises(OSError):                        # a busy/absent port is NOT a wrong-device None
        open_and_identify(c, _validate)


def test_success_first_attempt_has_no_extra_round_trips():
    c = _Conn(["OK"])
    assert open_and_identify(c, _validate) == ("SN", "FW")
    assert [o for o in c._ser_obj.ops].count(("idn",)) == 1


def test_posix_exclusive_kwargs_platform_gated(monkeypatch):
    monkeypatch.setattr(serial_connect.sys, "platform", "linux")
    assert posix_exclusive_kwargs() == {"exclusive": True}
    monkeypatch.setattr(serial_connect.sys, "platform", "win32")
    assert posix_exclusive_kwargs() == {}
