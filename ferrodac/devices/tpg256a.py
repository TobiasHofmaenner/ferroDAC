"""Pfeiffer Vacuum TPG 256 A (MaxiGauge) — the first real-hardware driver.

A 6-channel vacuum gauge controller on an RS-232 serial line. Protocol per the
operating manual (BG 805 186 BE): 8N1, no handshake.

    HOST → device :  "<MNEMONIC>[params]<CR><LF>"
    device → HOST :  <ACK><CR><LF>   (or <NAK> on error)
    HOST → device :  <ENQ>
    device → HOST :  "<data><CR><LF>"

Production notes
  - **Discovery is cached & non-blocking-ish**: each serial port is probed at
    most once (and never while we hold it); known verdicts are reused, so the
    2 s discovery loop doesn't re-poke ports or stall.
  - **One serial line, many accessors**: the poll loop reads and sink writes can
    interleave, so every exchange is guarded by a per-device lock.
  - **Self-healing**: a dropped link (USB yanked) yields NaN gaps and is
    transparently reopened (throttled) when the port returns.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..core.base import BaseDevice
from ..core.device import (
    Interface,
    Modality,
    RateControl,
    RateMode,
    Sink,
    SinkKind,
    Source,
    Status,
)

try:
    import serial
    import serial.tools.list_ports
    HAVE_SERIAL = True
except Exception:  # pragma: no cover
    serial = None
    HAVE_SERIAL = False

# --- control characters ----------------------------------------------------- #
ETX = b"\x03"
CR = b"\x0d"
LF = b"\x0a"
ENQ = b"\x05"
ACK = b"\x06"
NAK = b"\x15"

# 9600 is the firmware default; 19200 is the other common field setting.
PROBE_BAUDRATES: tuple[int, ...] = (9600, 19200)

UNIT_TEXT = {0: "mbar", 1: "Torr", 2: "Pa"}

GAUGE_TYPES = {
    "TPR": "Pirani", "PCR": "Pirani/Capacitance", "IKR": "Cold cathode",
    "IKR9": "Cold cathode", "IKR11": "Cold cathode", "PKR": "FullRange CC",
    "APR": "Linear", "CMR": "Capacitance", "ACR": "Capacitance",
    "IMR": "Pirani/High-p", "PBR": "FullRange BA",
}
# Gauge families whose emission can actually be switched on/off (SEN).
SWITCHABLE = ("IKR", "PKR", "PBR", "IMR")


class ProtocolError(Exception):
    pass


@dataclass
class GaugeInfo:
    channel: int
    ident: str
    present: bool
    label: str
    switchable: bool = False


@dataclass
class ProbeResult:
    port: str
    baud: int
    gauges: list           # list[GaugeInfo]
    unit: str = "mbar"
    firmware: str = ""
    states: list = field(default_factory=list)   # on/off per channel (0/1/2)


# --------------------------------------------------------------------------- #
#  Low-level serial link
# --------------------------------------------------------------------------- #
class _Link:
    """Wraps an open ``serial.Serial`` with the MaxiGauge request/response cycle."""

    def __init__(self, ser, port: str, baud: int):
        self.ser = ser
        self.port = port
        self.baud = baud

    def _send(self, mnemonic: str) -> None:
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write(mnemonic.encode("ascii") + CR + LF)
        self.ser.flush()
        resp = self.ser.read_until(expected=LF)
        if not resp:
            raise ProtocolError(f"no response to {mnemonic!r}")
        if resp[:1] == ACK:
            return
        if resp[:1] == NAK:
            raise ProtocolError(f"NAK on {mnemonic!r}")
        raise ProtocolError(f"unexpected reply to {mnemonic!r}: {resp!r}")

    def _enquire(self) -> str:
        self.ser.write(ENQ)
        self.ser.flush()
        line = self.ser.read_until(expected=LF)
        if not line:
            raise ProtocolError("no data after ENQ")
        return line.decode("ascii", "replace").strip("\r\n \t")

    def query(self, mnemonic: str) -> str:
        self._send(mnemonic)
        return self._enquire()

    def read_pressure(self, channel: int) -> tuple[int, float]:
        parts = self.query(f"PR{channel}").split(",")
        if len(parts) < 2:
            raise ProtocolError(f"bad PR{channel}: {parts!r}")
        try:
            return int(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            return int(parts[0].strip()), math.nan

    def read_identification(self) -> list[str]:
        return [t.strip() for t in self.query("TID").split(",")]

    def read_unit(self) -> str:
        try:
            return UNIT_TEXT.get(int(self.query("UNI").strip()), "mbar")
        except Exception:
            return "mbar"

    def read_firmware(self) -> str:
        try:
            return self.query("PNR")
        except Exception:
            return ""

    def read_states(self) -> list[int]:
        try:
            return [int(x) for x in self.query("SEN").split(",")[:6]]
        except Exception:
            return []

    def set_sensor(self, channel: int, on: bool) -> None:
        """Switch one gauge on/off via SEN (others = 0 / no change)."""
        params = ["0"] * 6
        params[channel - 1] = "2" if on else "1"
        self._send("SEN," + ",".join(params))
        self._enquire()       # consume the returned status line

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


def _open_serial(port: str, baud: int, timeout: float = 0.4):
    return serial.Serial(
        port, baud, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=timeout, write_timeout=timeout)


def _gauges_from_idents(idents: Sequence[str]) -> list:
    gauges = []
    for i, ident in enumerate(idents[:6], start=1):
        norm = ident.strip()
        low = norm.lower().replace(" ", "")
        if low.startswith("nosensor") or low in ("", "nosen"):
            gauges.append(GaugeInfo(i, norm, False, "—"))
        elif low.startswith("noident"):
            gauges.append(GaugeInfo(i, norm, True, "Unidentified"))
        else:
            label = GAUGE_TYPES.get(norm.upper(), norm)
            switch = any(norm.upper().startswith(s) for s in SWITCHABLE)
            gauges.append(GaugeInfo(i, norm, True, label, switch))
    return gauges


def list_ports() -> list[str]:
    if not HAVE_SERIAL:
        return []
    return [p.device for p in serial.tools.list_ports.comports()]


def probe_port(port: str, baudrates=PROBE_BAUDRATES) -> Optional[ProbeResult]:
    """Identify a TPG 256 A on a port; opens, identifies and *closes* (no hold)."""
    for baud in baudrates:
        try:
            ser = _open_serial(port, baud)
        except Exception:
            return None       # cannot open at all → give up on this port
        try:
            ser.write(ETX)
            ser.flush()
            time.sleep(0.05)
            ser.reset_input_buffer()
            ser.write(b"TID" + CR + LF)
            ser.flush()
            if ser.read_until(expected=LF)[:1] == ACK:
                ser.write(ENQ)
                ser.flush()
                data = ser.read_until(expected=LF).decode("ascii", "replace").strip()
                tokens = data.split(",")
                if len(tokens) == 6:        # six channels ⇒ a MaxiGauge
                    link = _Link(ser, port, baud)
                    res = ProbeResult(port, baud, _gauges_from_idents(tokens),
                                      link.read_unit(), link.read_firmware(),
                                      link.read_states())
                    ser.close()
                    return res
        except Exception:
            pass
        finally:
            if ser.is_open:
                ser.close()
    return None


# --------------------------------------------------------------------------- #
#  Device
# --------------------------------------------------------------------------- #
class TPG256ADevice(BaseDevice):
    driver = "tpg256a"
    discoverable = True

    _cache: dict = {}                # port -> ProbeResult | None
    _active_ports: set = set()       # ports we currently hold open
    _cls_lock = threading.Lock()

    def __init__(self, probe: ProbeResult):
        self._probe = probe
        self._port = probe.port
        self._baud = probe.baud
        self._gauges = probe.gauges
        present = [g for g in probe.gauges if g.present]

        sources = [
            Source(id=f"ch{g.channel}", name=f"CH{g.channel} {g.label}",
                   unit=probe.unit, modality=Modality.SCALAR, prefer_log=True)
            for g in present
        ]
        sinks = [
            Sink(id=f"sen{g.channel}", name=f"CH{g.channel} {g.label} on",
                 kind=SinkKind.TOGGLE,
                 value=(len(probe.states) >= g.channel
                        and probe.states[g.channel - 1] >= 2))
            for g in present if g.switchable
        ]
        super().__init__(
            instance_id=f"tpg:{probe.port}",
            name="TPG 256 A",
            interface=Interface(kind="rs232",
                                params={"port": probe.port, "baud": probe.baud}),
            sources=sources,
            sinks=sinks,
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=2.0,
                             default_hz=1.0, min_hz=0.1, max_hz=5.0),
            primary_source=sources[0].id if sources else None,
            hardware_id=f"TPG256A@{probe.port}",
            model="Pfeiffer MaxiGauge TPG 256 A",
        )
        self._unit = probe.unit
        self._link: Optional[_Link] = None
        self._io_lock = threading.Lock()
        self._last_reopen = 0.0

    # -- discovery -----------------------------------------------------------
    @classmethod
    def discover(cls):
        if not HAVE_SERIAL:
            return []
        present = set(list_ports())
        with cls._cls_lock:
            for p in [p for p in cls._cache if p not in present]:
                del cls._cache[p]
            to_probe = [p for p in present
                        if p not in cls._cache and p not in cls._active_ports]
        for p in to_probe:
            res = probe_port(p)                 # slow work outside the lock
            with cls._cls_lock:
                if p not in cls._active_ports:
                    cls._cache[p] = res
        with cls._cls_lock:
            results = [r for r in cls._cache.values() if r is not None]
        return [cls(r) for r in results]

    # -- lifecycle -----------------------------------------------------------
    def _connect(self) -> None:
        if not HAVE_SERIAL:
            raise RuntimeError("pyserial not available")
        self._open_link()
        self._firmware = self._probe.firmware
        with type(self)._cls_lock:
            type(self)._active_ports.add(self._port)
            type(self)._cache.pop(self._port, None)   # we hold it now

    def _disconnect(self) -> None:
        with self._io_lock:
            if self._link is not None:
                self._link.close()
                self._link = None
        with type(self)._cls_lock:
            type(self)._active_ports.discard(self._port)

    def _open_link(self) -> None:
        ser = _open_serial(self._port, self._baud)
        self._link = _Link(ser, self._port, self._baud)

    def _ensure_link(self) -> bool:
        """(Re)open the link if needed, throttled — returns True if usable."""
        if self._link is not None:
            return True
        now = time.monotonic()
        if now - self._last_reopen < 2.0:
            return False
        self._last_reopen = now
        try:
            self._open_link()
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    # -- data plane ----------------------------------------------------------
    def _read(self, source: Source):
        channel = int(source.id[2:])
        with self._io_lock:
            if not self._ensure_link():
                return math.nan, 1
            try:
                status, value = self._link.read_pressure(channel)
            except Exception as exc:
                self._drop_link(str(exc))
                return math.nan, 1
        if status == 0:
            return value, 0
        return math.nan, status          # underrange/overrange/off/no-sensor

    def _write(self, sink: Sink, value) -> None:
        channel = int(sink.id[3:])
        with self._io_lock:
            if not self._ensure_link():
                raise RuntimeError("TPG 256 A link is down")
            self._link.set_sensor(channel, bool(value))

    def _drop_link(self, msg: str) -> None:
        self._last_error = msg
        if self._link is not None:
            self._link.close()
            self._link = None
