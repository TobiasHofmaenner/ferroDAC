"""Keithley 6221 AC/DC current source — a bench current SOURCE (not a meter).

Two layers so the instrument logic is reusable outside ferroDAC (e.g. the LSA
calibration station drives the same source):

  * :class:`Keithley6221` — a dependency-free (pyserial only) SCPI controller with
    plain synchronous methods (``set_current``/``output``/``compliance``/``zero``).
    No ferroDAC/Qt imports — vendor or import it directly from a flasher/cal script.
  * :class:`Keithley6221Device` — the thin ferroDAC ``BaseDevice`` wrapper that
    exposes the controller as sinks (current setpoint, output, compliance, range)
    plus one source (the programmed output current) for logging.

Link (verified on a MODEL 6221, firmware A05): RS-232, **115200 8N1, no flow
control, <CR> terminator**. Enable RS-232 on the unit (COMM menu) or it stays
silent. SCPI:

    *IDN?                      -> "KEITHLEY INSTRUMENTS INC.,MODEL 6221,<sn>,<fw>"
    *RST / *CLS                reset / clear status
    SOUR:CURR <A> / ?          DC output current level (±105 mA on the 6221)
    SOUR:CURR:RANG <A> / ?     output range;  SOUR:CURR:RANG:AUTO ON|OFF / ?
    SOUR:CURR:COMP <V> / ?     compliance (voltage limit)
    OUTP ON|OFF / ?            output relay
    SYST:ERR?                  -> "0,\"No error\"" | "-222,\"Parameter data out of range\""

Out-of-range levels are REJECTED by the instrument (error -222) and leave the
level unchanged, so the controller validates against the model limit first.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..core.base import BaseDevice
from ..core.device import (
    Interface,
    Modality,
    Param,
    RateControl,
    RateMode,
    Sink,
    SinkKind,
    Source,
)

try:
    import serial
    import serial.tools.list_ports
    HAVE_SERIAL = True
except Exception:  # pragma: no cover
    serial = None
    HAVE_SERIAL = False

BAUD = 115200
TERM = b"\r"
# Model 6221 spec: ±105 mA max output, 100 fA resolution; compliance 0.1–105 V.
MAX_CURRENT = 0.105
MIN_COMPLIANCE = 0.1
MAX_COMPLIANCE = 105.0


class Keithley6221Error(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Reusable, dependency-free SCPI controller
# --------------------------------------------------------------------------- #
class Keithley6221:
    """Synchronous SCPI control of a 6221 current source over a serial line.

    Pure pyserial — safe to import from non-ferroDAC code. Use as a context
    manager or call :meth:`open`/:meth:`close`::

        with Keithley6221("/dev/ttyUSB0") as k:
            k.compliance(5.0)
            k.current(1e-6)
            k.output(True)
            ...
            k.zero()            # 0 A + output off (leave the source safe)
    """

    def __init__(self, port: str, baud: int = BAUD, timeout: float = 1.5):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    # -- link ---------------------------------------------------------------- #
    def open(self) -> "Keithley6221":
        if not HAVE_SERIAL:
            raise Keithley6221Error("pyserial not available")
        self._ser = serial.Serial(
            self.port, self.baud, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=self.timeout, write_timeout=self.timeout)
        time.sleep(0.15)  # let the USB-serial adapter settle after open
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        return self

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> "Keithley6221":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _write(self, cmd: str) -> None:
        if self._ser is None:
            raise Keithley6221Error("port not open")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(cmd.encode("ascii") + TERM)
            self._ser.flush()
        except Exception as exc:  # pragma: no cover - hardware I/O
            raise Keithley6221Error(f"write {cmd!r} failed: {exc}") from exc

    def _query(self, cmd: str) -> str:
        self._write(cmd)
        try:
            resp = self._ser.read_until(TERM)
        except Exception as exc:  # pragma: no cover - hardware I/O
            raise Keithley6221Error(f"read after {cmd!r} failed: {exc}") from exc
        text = resp.decode("ascii", "replace").strip("\r\n \t")
        if not text:
            raise Keithley6221Error(f"no response to {cmd!r}")
        return text

    # -- identity / housekeeping -------------------------------------------- #
    def idn(self) -> str:
        return self._query("*IDN?")

    def reset(self) -> None:
        """*CLS + *RST: clear status and return to a known state (0 A, output off)."""
        self._write("*CLS")
        self._write("*RST")
        time.sleep(0.2)

    def clear_status(self) -> None:
        """*CLS: clear the status byte + error queue WITHOUT touching the source state."""
        self._write("*CLS")

    def error(self) -> tuple[int, str]:
        """Pop one entry off the error queue -> (code, message). (0, 'No error') when clear.
        A response whose code can't be parsed is treated as an ERROR (raised), not silently
        as 'no error' — this is the post-write safety check, so it must fail loud."""
        raw = self._query("SYST:ERR?")
        code, _, msg = raw.partition(",")
        try:
            return int(float(code)), msg.strip().strip('"')
        except ValueError as exc:
            raise Keithley6221Error(f"unparseable SYST:ERR? response {raw!r}") from exc

    def raise_on_error(self) -> None:
        code, msg = self.error()
        if code != 0:
            raise Keithley6221Error(f"instrument error {code}: {msg}")

    # -- current ------------------------------------------------------------- #
    def current(self, amps: float) -> None:
        """Set the DC output current level (A). Validated against the model limit so
        an out-of-range value fails loudly here instead of being silently rejected."""
        if not math.isfinite(amps) or abs(amps) > MAX_CURRENT:
            raise Keithley6221Error(
                f"current {amps} A out of range (±{MAX_CURRENT} A)")
        self._write(f"SOUR:CURR {amps:.6E}")

    def get_current(self) -> float:
        return float(self._query("SOUR:CURR?"))

    # -- output relay -------------------------------------------------------- #
    def output(self, on: bool) -> None:
        self._write("OUTP ON" if on else "OUTP OFF")

    def get_output(self) -> bool:
        return self._query("OUTP?").startswith("1")

    # -- compliance ---------------------------------------------------------- #
    def compliance(self, volts: float) -> None:
        if not (MIN_COMPLIANCE <= volts <= MAX_COMPLIANCE):
            raise Keithley6221Error(
                f"compliance {volts} V out of range ({MIN_COMPLIANCE}–{MAX_COMPLIANCE} V)")
        self._write(f"SOUR:CURR:COMP {volts:.6E}")

    def get_compliance(self) -> float:
        return float(self._query("SOUR:CURR:COMP?"))

    # -- range --------------------------------------------------------------- #
    def range_auto(self, on: bool) -> None:
        self._write(f"SOUR:CURR:RANG:AUTO {'ON' if on else 'OFF'}")

    def get_range_auto(self) -> bool:
        return self._query("SOUR:CURR:RANG:AUTO?").startswith("1")

    def set_range(self, amps: float) -> None:
        self._write(f"SOUR:CURR:RANG {amps:.6E}")

    def get_range(self) -> float:
        return float(self._query("SOUR:CURR:RANG?"))

    # -- safety -------------------------------------------------------------- #
    def zero(self) -> None:
        """Leave the source safe: 0 A then output off."""
        self.current(0.0)
        self.output(False)


# --------------------------------------------------------------------------- #
#  Discovery
# --------------------------------------------------------------------------- #
@dataclass
class ProbeResult:
    port: str
    model: str = "6221"
    firmware: str = ""
    serial: str = ""            # instrument serial (from *IDN?), port-independent identity
    usb_serial: str = ""        # USB adapter serial (fallback identity)


def _parse_idn(idn: str) -> Optional[tuple[str, str, str]]:
    """('6221','<sn>','<fw>') from a 6221 *IDN?, else None."""
    parts = [p.strip() for p in idn.split(",")]
    if len(parts) < 4 or "6221" not in parts[1].upper():
        return None
    model = parts[1].upper().replace("MODEL", "").strip() or "6221"
    fw = parts[3].split()                        # a truncated IDN must not IndexError
    return model, parts[2], (fw[0] if fw else "")


def probe_port(port: str) -> Optional[ProbeResult]:
    """Identify a 6221 on a port; opens, identifies and *closes* (never holds it)."""
    if not HAVE_SERIAL:
        return None
    try:
        k = Keithley6221(port, timeout=0.6).open()
    except Exception:
        return None
    try:
        parsed = _parse_idn(k.idn())
        if parsed is None:
            return None
        model, sn, fw = parsed
        return ProbeResult(port=port, model=model, firmware=fw, serial=sn)
    except Exception:
        return None
    finally:
        k.close()


# --------------------------------------------------------------------------- #
#  ferroDAC device wrapper
# --------------------------------------------------------------------------- #
class Keithley6221Device(BaseDevice):
    driver = "keithley6221"
    discoverable = True

    _cache: dict = {}                # port -> ProbeResult | None
    _active_ports: set = set()       # ports we currently hold open
    _cls_lock = threading.Lock()

    def __init__(self, probe: ProbeResult):
        self._probe = probe
        self._port = probe.port
        sources = [
            Source(id="iout", name="Output current", unit="A",
                   modality=Modality.SCALAR, prefer_log=False),
        ]
        sinks = [
            Sink(id="current", name="Current", kind=SinkKind.SETPOINT,
                 params=(Param("current", "float", "A",
                               minimum=-MAX_CURRENT, maximum=MAX_CURRENT),),
                 value=0.0),
            Sink(id="output", name="Output", kind=SinkKind.TOGGLE, value=False),
            Sink(id="compliance", name="Compliance", kind=SinkKind.SETPOINT,
                 params=(Param("voltage", "float", "V",
                               minimum=MIN_COMPLIANCE, maximum=MAX_COMPLIANCE),),
                 value=10.0),
            Sink(id="range_auto", name="Auto range", kind=SinkKind.TOGGLE, value=True),
            Sink(id="zero", name="Zero output", kind=SinkKind.ACTION),
        ]
        super().__init__(
            instance_id=f"k6221:{probe.serial or probe.port}",
            name="Keithley 6221",
            interface=Interface(kind="rs232", params={"port": probe.port, "baud": BAUD}),
            sources=sources,
            sinks=sinks,
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=2.0,
                             default_hz=1.0, min_hz=0.1, max_hz=5.0),
            primary_source="iout",
            hardware_id=f"KEITHLEY6221:{probe.serial or probe.usb_serial or probe.port}",
            model="Keithley 6221 AC/DC Current Source",
            manufacturer="Keithley",
        )
        self._firmware = probe.firmware or None
        self._k: Optional[Keithley6221] = None
        self._io_lock = threading.Lock()

    # -- discovery ----------------------------------------------------------- #
    @classmethod
    def discover(cls):
        if not HAVE_SERIAL:
            return []
        usb_serials = {p.device: (p.serial_number or "")
                       for p in serial.tools.list_ports.comports()}
        present = set(usb_serials)
        with cls._cls_lock:
            for p in [p for p in cls._cache if p not in present]:
                del cls._cache[p]
            to_probe = [p for p in present
                        if p not in cls._cache and p not in cls._active_ports]
        for p in to_probe:
            res = probe_port(p)                       # slow work outside the lock
            if res is not None:
                res.usb_serial = usb_serials.get(p, "")
            with cls._cls_lock:
                if p not in cls._active_ports:
                    cls._cache[p] = res
        with cls._cls_lock:
            results = [r for r in cls._cache.values() if r is not None]
        return [cls(r) for r in results]

    # -- lifecycle ----------------------------------------------------------- #
    def _connect(self) -> None:
        if not HAVE_SERIAL:
            raise RuntimeError("pyserial not available")
        if self._k is not None:                   # reconnect w/o disconnect → don't leak the handle
            try:
                self._k.close()
            finally:
                self._k = None
        k = Keithley6221(self._port).open()
        try:
            parsed = _parse_idn(k.idn())
            if parsed is None:
                raise RuntimeError("not a Keithley 6221 on this port")
            self._firmware = parsed[2]
            k.clear_status()                      # drop a stale error queue (no state change)
            # Seed the sink values from the REAL instrument state so describe()/the panel
            # reflect what the source is ACTUALLY doing (least surprise; the tpg256a
            # convention). We deliberately do NOT *RST — an operator's live run is preserved,
            # but it's shown truthfully rather than as a hardcoded "off".
            self._sink_values["output"] = k.get_output()
            self._sink_values["current"] = k.get_current()
            self._sink_values["compliance"] = k.get_compliance()
            self._sink_values["range_auto"] = k.get_range_auto()
        except Exception:
            k.close()
            raise
        self._k = k
        with type(self)._cls_lock:
            type(self)._active_ports.add(self._port)
            type(self)._cache.pop(self._port, None)   # we hold it now

    def _disconnect(self) -> None:
        # Leave the instrument's output state as the operator set it (least surprise);
        # the "Zero output" sink is there for an explicit safe-off.
        with self._io_lock:
            if self._k is not None:
                self._k.close()
                self._k = None
        with type(self)._cls_lock:
            type(self)._active_ports.discard(self._port)

    # -- control ------------------------------------------------------------- #
    def write(self, sink_id: str, value=None) -> None:
        # REJECT an out-of-range current/compliance loudly here, BEFORE BaseDevice.write()
        # silently CLAMPS the SETPOINT to the Param's full scale — for a current source,
        # "asked for 1 A, quietly sourced 105 mA" is a data-integrity/safety trap.
        if sink_id == "current" and value is not None:
            a = float(value)
            if not math.isfinite(a) or abs(a) > MAX_CURRENT:
                raise ValueError(f"current {value} A out of range (±{MAX_CURRENT} A)")
        elif sink_id == "compliance" and value is not None:
            v = float(value)
            if not (MIN_COMPLIANCE <= v <= MAX_COMPLIANCE):
                raise ValueError(f"compliance {value} V out of range "
                                 f"({MIN_COMPLIANCE}–{MAX_COMPLIANCE} V)")
        super().write(sink_id, value)

    # -- data plane ---------------------------------------------------------- #
    def _read(self, source: Source):
        # The 6221 is a source, not a meter: report the current it is programmed to
        # deliver (0 when the output relay is open) so it can be logged/plotted.
        with self._io_lock:
            if self._k is None:
                return math.nan, 1
            try:
                on = self._k.get_output()
                return (self._k.get_current() if on else 0.0), 0
            except Exception:
                return math.nan, 1

    def _write(self, sink: Sink, value) -> None:
        with self._io_lock:
            if self._k is None:
                raise RuntimeError("Keithley 6221 link is down")
            if sink.id == "current":
                self._k.current(float(value))
            elif sink.id == "output":
                self._k.output(bool(value))
            elif sink.id == "compliance":
                self._k.compliance(float(value))
            elif sink.id == "range_auto":
                self._k.range_auto(bool(value))
            elif sink.id == "zero":
                self._k.zero()
                self._sink_values["output"] = False   # reflect the safe-off in tracked state
                self._sink_values["current"] = 0.0     # (ACTION → BaseDevice won't track it)
            else:
                raise RuntimeError(f"unknown sink {sink.id!r}")
            self._k.raise_on_error()
