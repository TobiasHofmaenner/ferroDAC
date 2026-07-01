"""Keithley 6221 current-source driver: a reusable pyserial-free-of-ferroDAC
controller (Keithley6221) plus a thin BaseDevice wrapper. Exercised here with a
fake serial port that emulates the 6221's SCPI (115200/CR) — no hardware."""
import pytest

pytest.importorskip("serial")  # driver needs pyserial; skip where it isn't installed

import ferrodac.devices.keithley6221 as mod  # noqa: E402
from ferrodac.devices.keithley6221 import (
    Keithley6221,
    Keithley6221Device,
    Keithley6221Error,
)

IDN = "KEITHLEY INSTRUMENTS INC.,MODEL 6221,1214209,A05  /700x"


class FakeSerial:
    """Minimal 6221 emulator: holds source state, answers the queries we use."""

    def __init__(self, *a, idn=IDN, **k):
        self.state = {"curr": 0.0, "outp": 0, "comp": 10.0, "rng": 0.1, "auto": 1}
        self.err = (0, "No error")
        self._idn = idn
        self._out = b""
        self.is_open = True

    # -- serial API surface the controller uses --
    def reset_input_buffer(self):
        self._out = b""

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def write(self, data: bytes):
        line = data.decode().strip()
        self._out = self._respond(line).encode() if line.endswith("?") else b""
        if not line.endswith("?"):
            self._apply(line)
        return len(data)

    def read_until(self, expected=b"\r"):
        out, self._out = self._out, b""
        return out

    # -- emulation --
    def _apply(self, line: str):
        u = line.upper()
        try:
            if u.startswith("SOUR:CURR:COMP"):
                self.state["comp"] = float(line.split()[-1])
            elif u.startswith("SOUR:CURR:RANG:AUTO"):
                self.state["auto"] = 1 if line.split()[-1].upper() == "ON" else 0
            elif u.startswith("SOUR:CURR:RANG"):
                self.state["rng"] = float(line.split()[-1])
            elif u.startswith("SOUR:CURR"):
                v = float(line.split()[-1])
                if abs(v) > 0.105:                       # instrument rejects out-of-range
                    self.err = (-222, "Parameter data out of range")
                else:
                    self.state["curr"] = v
            elif u.startswith("OUTP"):
                self.state["outp"] = 1 if "ON" in u else 0
            elif u in ("*RST", "*CLS"):
                self.state.update(curr=0.0, outp=0)
                self.err = (0, "No error")
        except (ValueError, IndexError):
            self.err = (-222, "Parameter data out of range")

    def _respond(self, line: str) -> str:
        u = line.upper()
        if u == "*IDN?":
            return self._idn + "\r"
        if u == "SYST:ERR?":
            code, msg = self.err
            self.err = (0, "No error")
            return f'{code},"{msg}"\r'
        if u == "SOUR:CURR:COMP?":
            return f"{self.state['comp']:.6E}\r"
        if u == "SOUR:CURR:RANG:AUTO?":
            return f"{self.state['auto']}\r"
        if u == "SOUR:CURR:RANG?":
            return f"{self.state['rng']:.6E}\r"
        if u == "SOUR:CURR?":
            return f"{self.state['curr']:.5E}\r"
        if u == "OUTP?":
            return f"{self.state['outp']}\r"
        return "\r"


def _patch(monkeypatch, **kw):
    monkeypatch.setattr(mod, "HAVE_SERIAL", True)
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod.serial, "Serial", lambda *a, **k: FakeSerial(*a, **kw))
    monkeypatch.setattr(
        mod.serial.tools.list_ports, "comports",
        lambda: [type("P", (), {"device": "/dev/ttyUSB0", "serial_number": "usb42"})()],
    )


def test_controller_roundtrip(monkeypatch):
    _patch(monkeypatch)
    with Keithley6221("/dev/ttyUSB0") as k:
        assert "6221" in k.idn()
        k.reset()
        k.compliance(5.0)
        assert k.get_compliance() == 5.0
        k.current(1e-6)
        assert k.get_current() == 1e-6
        k.output(True)
        assert k.get_output() is True
        k.current(-2e-5)
        assert k.get_current() == -2e-5
        k.zero()
        assert k.get_output() is False and k.get_current() == 0.0
        assert k.error() == (0, "No error")


def test_controller_rejects_out_of_range(monkeypatch):
    _patch(monkeypatch)
    with Keithley6221("/dev/ttyUSB0") as k:
        for bad in (1.0, -0.2, float("nan")):
            try:
                k.current(bad)
            except Keithley6221Error:
                continue
            raise AssertionError(f"{bad} should have been rejected")


def test_compliance_bounds(monkeypatch):
    _patch(monkeypatch)
    with Keithley6221("/dev/ttyUSB0") as k:
        for bad in (0.0, 200.0):
            try:
                k.compliance(bad)
            except Keithley6221Error:
                continue
            raise AssertionError(f"compliance {bad} should have been rejected")


def test_discover_and_describe(monkeypatch):
    _patch(monkeypatch)
    Keithley6221Device._cache.clear()
    Keithley6221Device._active_ports.clear()
    devs = Keithley6221Device.discover()
    assert len(devs) == 1
    d = devs[0].describe()
    assert d.instance_id == "k6221:1214209"
    assert d.model.startswith("Keithley 6221") and d.firmware == "A05"
    assert d.hardware_id == "KEITHLEY6221:1214209"
    assert {s.id for s in d.sinks} == {"current", "output", "compliance", "range_auto", "zero"}
    assert [s.id for s in d.sources] == ["iout"]


def test_discover_ignores_non_6221(monkeypatch):
    _patch(monkeypatch, idn="Some Other Instrument,MODEL 2000,x,y")
    Keithley6221Device._cache.clear()
    Keithley6221Device._active_ports.clear()
    assert Keithley6221Device.discover() == []


def test_device_write_read(monkeypatch):
    _patch(monkeypatch)
    Keithley6221Device._cache.clear()
    Keithley6221Device._active_ports.clear()
    d = Keithley6221Device.discover()[0]
    d.connect()
    src = d.describe().sources[0]
    d.write("compliance", 4.0)
    d.write("current", 5e-6)
    d.write("output", True)
    val, st = d._read(src)
    assert st == 0 and val == 5e-6                # output on -> reports programmed current
    d.write("zero")
    val, st = d._read(src)
    assert st == 0 and val == 0.0                 # output off -> 0
    d.disconnect()
