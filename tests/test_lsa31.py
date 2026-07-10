"""Ferrovac LSA3.1 ion-pump controller driver: the dependency-free LSA31
controller plus the BaseDevice wrapper, exercised against a fake serial port
that emulates the instrument's line protocol (115200/LF) — no hardware.
Protocol reference: SWFWLSA3.1 docs/programming-manual.md rev A."""
import math

import pytest

pytest.importorskip("serial")

import ferrodac.devices.lsa31 as mod  # noqa: E402
from ferrodac.devices.lsa31 import (  # noqa: E402
    LSA31,
    LSA31Device,
    LSA31Error,
    ST_BATT_CRIT,
    ST_HV_ON,
    ST_NO_TSENS,
    ST_OVERLOAD,
    _parse_idn,
    probe_port,
)

IDN = "Ferrovac,LSA3.1,LSA31-000042,0.7.0"


class FakeSerial:
    """Minimal LSA3.1 emulator: answers the commands the driver uses, one line
    in → one line out (manual §4.1), LF-terminated."""

    def __init__(self, *a, idn=IDN, **k):
        self.state = {
            "outp": 0, "sens": float("nan"), "filt": 4,
            "curr": 8.9e-8, "hv": 5.002e3, "temp": 22.8, "vbat": 7.91,
            "status": 0x00, "refuse_outp": False,
        }
        self._idn = idn
        self._out = b""
        self.meas_count = 0                    # MEAS? transactions (cache test)
        self.is_open = True

    # -- pyserial surface the controller touches --
    def reset_input_buffer(self):
        self._out = b""

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def write(self, data: bytes):
        line = data.decode().strip()
        self._out += (self._respond(line) + "\n").encode()
        return len(data)

    def read_until(self, term):
        out, self._out = self._out, b""
        return out

    # -- the instrument --
    def _num(self, v):
        return f"{v:.5e}" if not math.isnan(v) else "nan"

    def _respond(self, line: str) -> str:
        cmd = line.upper()
        s = self.state
        if cmd == "*IDN?":
            return self._idn
        if cmd == "*CLS":
            return "OK"
        if cmd == "MEAS?":
            self.meas_count += 1
            status = s["status"] | (ST_HV_ON if s["outp"] else 0)
            pres = s["curr"] * s["sens"] if not math.isnan(s["sens"]) else float("nan")
            return (f"{self._num(s['curr'])},{self._num(pres)},"
                    f"{self._num(s['hv'])},{self._num(s['temp'])},"
                    f"{self._num(s['vbat'])},0x{status:02x}")
        if cmd in ("OUTP ON", "OUTP OFF"):
            if s["refuse_outp"]:
                return 'ERR,4,"Command refused"'
            s["outp"] = 1 if cmd.endswith("ON") else 0
            return "OK"
        if cmd == "OUTP?":
            return str(s["outp"])
        if cmd.startswith("CONF:PUMP:SENS "):
            s["sens"] = float(line.split(" ", 1)[1])
            return "OK"
        if cmd == "CONF:PUMP:SENS?":
            return self._num(s["sens"])
        if cmd.startswith("CONF:FILT "):
            n = int(line.split(" ", 1)[1])
            if not 1 <= n <= 16:
                return 'ERR,3,"Parameter out of range"'
            s["filt"] = n
            return "OK"
        if cmd == "CONF:FILT?":
            return str(s["filt"])
        if cmd == "SYST:ERR?":
            return '0,"No error"'
        return 'ERR,1,"Unknown command"'


@pytest.fixture
def fake(monkeypatch):
    """Route LSA31.open at a FakeSerial; returns the shared instance."""
    fs = FakeSerial()
    monkeypatch.setattr(mod.serial, "Serial", lambda *a, **k: fs)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return fs


# -- controller ------------------------------------------------------------- #

def test_idn_and_meas_parse(fake):
    with LSA31("/dev/ttyACM0") as lsa:
        assert lsa.idn() == IDN
        m = lsa.meas()
        assert m.current == pytest.approx(8.9e-8)
        assert math.isnan(m.pressure)          # no sensitivity configured
        assert m.voltage == pytest.approx(5002.0)
        assert m.status == 0x00


def test_refused_output_raises_never_silent(fake):
    """ERR,4 (battery critical etc.) must raise — the UI may never believe an
    enable the instrument declined (the QMS-filament audit lesson)."""
    fake.state["refuse_outp"] = True
    with LSA31("/dev/ttyACM0") as lsa:
        with pytest.raises(LSA31Error, match="error 4"):
            lsa.output(True)
        assert fake.state["outp"] == 0


def test_1200_baud_is_rejected_as_bootloader_trigger(fake):
    with pytest.raises(LSA31Error, match="bootloader"):
        LSA31("/dev/ttyACM0", baud=1200)


def test_sensitivity_makes_pressure_live(fake):
    with LSA31("/dev/ttyACM0") as lsa:
        lsa.set_sensitivity(2.7e4)
        m = lsa.meas()
        assert m.pressure == pytest.approx(8.9e-8 * 2.7e4)


# -- identity / discovery ----------------------------------------------------- #

def test_parse_idn_accepts_lsa31_only():
    assert _parse_idn(IDN) == ("LSA31-000042", "0.7.0")
    assert _parse_idn("KEITHLEY INSTRUMENTS INC.,MODEL 6221,x,y") is None
    assert _parse_idn("Ferrovac,TPG300,x,y") is None
    assert _parse_idn("garbage") is None


def test_probe_port_identifies_and_closes(fake):
    res = probe_port("/dev/ttyACM0")
    assert res is not None and res.serial == "LSA31-000042"
    assert res.firmware == "0.7.0"
    assert not fake.is_open                    # probe never holds the port


# -- BaseDevice wrapper -------------------------------------------------------- #

def _device(fake) -> LSA31Device:
    dev = LSA31Device(mod.ProbeResult(port="/dev/ttyACM0",
                                      serial="LSA31-000042", firmware="0.7.0"))
    dev._connect()
    return dev


def _read(dev, sid):
    src = next(s for s in dev._sources if s.id == sid)
    return dev._read(src)


def test_device_reads_all_channels_from_one_transaction(fake):
    dev = _device(fake)
    fake.meas_count = 0
    values = {sid: _read(dev, sid) for sid in
              ("current", "pressure", "hv", "temp", "vbat")}
    assert fake.meas_count == 1                # ONE MEAS? served the whole cycle
    assert values["current"] == (pytest.approx(8.9e-8), 0)
    assert values["hv"] == (pytest.approx(5002.0), 0)
    assert values["vbat"] == (pytest.approx(7.91), 0)
    v, st = values["pressure"]
    assert math.isnan(v) and st == 0           # honest nan until sensitivity set


def test_overload_invalidates_current_and_pressure_only(fake):
    dev = _device(fake)
    fake.state["status"] = ST_OVERLOAD
    fake.state["sens"] = 2.7e4
    dev._meas = None                           # force a re-query
    v, st = _read(dev, "current")
    assert math.isnan(v) and st == 1
    v, st = _read(dev, "pressure")
    assert math.isnan(v) and st == 1
    v, st = _read(dev, "hv")                   # HV/vbat stay valid
    assert v == pytest.approx(5002.0) and st == 0


def test_no_temp_sensor_flags_temp_bad(fake):
    dev = _device(fake)
    fake.state["status"] = ST_NO_TSENS
    fake.state["temp"] = float("nan")
    dev._meas = None
    v, st = _read(dev, "temp")
    assert math.isnan(v) and st == 1


def test_battery_critical_is_a_warning_not_bad_data(fake):
    dev = _device(fake)
    fake.state["status"] = ST_BATT_CRIT
    dev._meas = None
    v, st = _read(dev, "vbat")
    assert v == pytest.approx(7.91) and st == 0


def test_hv_sink_roundtrip_and_seeding(fake):
    fake.state["outp"] = 1                     # instrument already running
    dev = _device(fake)
    assert dev._sink_values["hv_enable"] is True   # seeded from the REAL state
    sink = next(s for s in dev._sinks if s.id == "hv_enable")
    dev._write(sink, False)
    assert fake.state["outp"] == 0


def test_refused_hv_write_raises_through_the_device(fake):
    dev = _device(fake)
    fake.state["refuse_outp"] = True
    sink = next(s for s in dev._sinks if s.id == "hv_enable")
    with pytest.raises(LSA31Error, match="error 4"):
        dev._write(sink, True)


def test_options_apply_to_the_instrument(fake):
    dev = _device(fake)
    dev._on_option("pump_sens", "2.7e4")
    assert fake.state["sens"] == pytest.approx(2.7e4)
    dev._on_option("filter", "8")
    assert fake.state["filt"] == 8
    with pytest.raises(LSA31Error):            # ERR,3 surfaces, never swallowed
        dev._on_option("filter", "99")
