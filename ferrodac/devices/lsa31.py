"""Ferrovac LSA3.1 ion-pump HV controller — portable UHV pump monitor/control.

Two layers, following the keithley6221 pattern, so the instrument logic is
reusable outside ferroDAC (cal stations, flashing scripts):

  * :class:`LSA31` — a dependency-free (pyserial only) controller speaking the
    LSA3.1's line-oriented SCPI-style ASCII protocol (see the firmware repo's
    ``docs/programming-manual.md``). Plain synchronous methods; no ferroDAC/Qt.
  * :class:`LSA31Device` — the thin ferroDAC ``BaseDevice`` wrapper: five scalar
    sources sharing one acquisition instant, an HV-output toggle sink, and pump
    sensitivity / averaging-filter options.

Link: USB CDC virtual COM port (or the rear UART), **115200 8N1, no flow
control, LF terminator**. Every command answers exactly one line within ~0.8 s.

    *IDN?          -> "Ferrovac,LSA3.1,<serial>,<fw>"
    MEAS?          -> "<I_A>,<P_mbar>,<HV_V>,<T_C>,<Vbat_V>,<0xSS>"   (one instant)
    OUTP ON|OFF    -> "OK" | 'ERR,4,"..."' (refused, e.g. battery critical)
    CONF:PUMP:SENS <S> / CONF:FILT <n>     pump sensitivity / averaging window

SAFETY (from the manual, §3.1): NEVER open the USB port at 1200 baud — on this
hardware a 1200-baud open is the bootloader-activation signal and resets the
instrument into firmware-update mode. This driver only ever opens at 115200.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

from ..core.base import BaseDevice
from ..core.serial_arbiter import PORTS_IN_USE, SERIAL_LOCK
from ..core.serial_connect import open_and_identify, posix_exclusive_kwargs
from ..core.device import (
    Interface,
    Modality,
    Option,
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

BAUD = 115200        # NEVER 1200 — that is the bootloader trigger (manual §3.1)
TERM = b"\n"

# MEAS? status byte (manual §5.3)
ST_OVERLOAD = 0x01   # current above positive full scale — reading invalid
ST_UNDERRANGE = 0x02  # current below negative full scale — reading invalid
ST_NO_TSENS = 0x04   # no temperature sensor fitted
ST_HV_ON = 0x08      # state flag, not a fault
ST_BATT_LOW = 0x10   # warning — data still valid
ST_BATT_CRIT = 0x20  # shut-off imminent — data still valid
ST_SETTLING = 0x40   # readings stabilising after power-on


class LSA31Error(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Reusable, dependency-free controller
# --------------------------------------------------------------------------- #
class LSA31:
    """Synchronous control of an LSA3.1 over its serial/USB-CDC line.

    Pure pyserial — safe to import from non-ferroDAC code::

        with LSA31("/dev/ttyACM0") as lsa:
            print(lsa.idn())
            m = lsa.meas()
            if m.status & ST_BATT_CRIT:
                ...
    """

    def __init__(self, port: str, baud: int = BAUD, timeout: float = 1.0):
        if baud == 1200:                       # defence in depth (manual §3.1)
            raise LSA31Error("1200 baud is the LSA3.1 bootloader trigger")
        self.port = port
        self.baud = baud
        self.timeout = timeout                 # manual §4.3: answers within ~0.8 s
        self._ser = None

    # -- link ----------------------------------------------------------------- #
    def open(self) -> "LSA31":
        if not HAVE_SERIAL:
            raise LSA31Error("pyserial not available")
        self._ser = serial.Serial(
            self.port, self.baud, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=self.timeout, write_timeout=self.timeout,
            **posix_exclusive_kwargs())        # exclusive on POSIX (issue #6) — was missing here
        time.sleep(0.15)                       # let the CDC device settle after open
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

    def __enter__(self) -> "LSA31":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _exchange(self, line: str) -> str:
        """One command line -> one response line (manual §4.1/§4.2)."""
        if self._ser is None:
            raise LSA31Error("port not open")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(line.encode("ascii") + TERM)
            self._ser.flush()
            resp = self._ser.read_until(TERM)
        except Exception as exc:  # pragma: no cover - hardware I/O
            raise LSA31Error(f"exchange {line!r} failed: {exc}") from exc
        text = resp.decode("ascii", "replace").strip("\r\n \t")
        if not text:
            raise LSA31Error(f"no response to {line!r}")
        return text

    def query(self, line: str) -> str:
        """A `...?` query: returns the data line (which is never `ERR,...`
        for a well-formed query, but check anyway so a refused/unknown query
        fails loudly instead of being parsed as data)."""
        resp = self._exchange(line)
        if resp.startswith("ERR,"):
            code, msg = self._parse_err(resp)
            raise LSA31Error(f"instrument error {code}: {msg}")
        return resp

    def command(self, line: str) -> str:
        """A set command: expects `OK` (or `OK,...`), raises on `ERR,...`."""
        resp = self._exchange(line)
        if resp.startswith("ERR,"):
            code, msg = self._parse_err(resp)
            raise LSA31Error(f"instrument error {code}: {msg}")
        if not resp.startswith("OK"):
            raise LSA31Error(f"unexpected response to {line!r}: {resp!r}")
        return resp

    @staticmethod
    def _parse_err(resp: str) -> tuple[int, str]:
        parts = resp.split(",", 2)             # ERR,<code>,"<message>"
        try:
            code = int(parts[1])
        except (IndexError, ValueError):
            code = -1
        msg = parts[2].strip().strip('"') if len(parts) > 2 else ""
        return code, msg

    # -- identity / housekeeping ---------------------------------------------- #
    def idn(self) -> str:
        return self.query("*IDN?")

    def clear_status(self) -> None:
        self.command("*CLS")

    def error(self) -> tuple[int, str]:
        """Read + clear the last-error slot -> (code, message)."""
        raw = self.query("SYST:ERR?")
        code, _, msg = raw.partition(",")
        try:
            return int(code), msg.strip().strip('"')
        except ValueError as exc:
            raise LSA31Error(f"unparseable SYST:ERR? response {raw!r}") from exc

    # -- measurement ----------------------------------------------------------- #
    def meas(self) -> "Meas":
        """All channels in ONE acquisition instant (manual §6.2 — the
        recommended polling command)."""
        raw = self.query("MEAS?")
        f = raw.split(",")
        if len(f) != 6:
            raise LSA31Error(f"malformed MEAS? response {raw!r}")
        try:
            return Meas(current=float(f[0]), pressure=float(f[1]),
                        voltage=float(f[2]), temperature=float(f[3]),
                        battery=float(f[4]), status=int(f[5], 16))
        except ValueError as exc:
            raise LSA31Error(f"malformed MEAS? response {raw!r}") from exc

    # -- high voltage ----------------------------------------------------------- #
    def output(self, on: bool) -> None:
        """Enable/disable the HV output. Raises LSA31Error code 4 when the
        instrument REFUSES (e.g. battery critical) — never swallow that."""
        self.command("OUTP ON" if on else "OUTP OFF")

    def get_output(self) -> bool:
        return self.query("OUTP?").strip().startswith("1")

    # -- configuration ----------------------------------------------------------- #
    def set_sensitivity(self, mbar_per_a: float) -> None:
        self.command(f"CONF:PUMP:SENS {mbar_per_a:.6e}")

    def get_sensitivity(self) -> float:
        return float(self.query("CONF:PUMP:SENS?"))

    def set_filter(self, n: int) -> None:
        self.command(f"CONF:FILT {int(n)}")

    def get_filter(self) -> int:
        return int(self.query("CONF:FILT?"))


@dataclass(frozen=True)
class Meas:
    """One MEAS? line — all channels share the acquisition instant."""
    current: float       # A
    pressure: float      # mbar (nan until a pump sensitivity is configured)
    voltage: float       # V
    temperature: float   # °C (nan when no sensor is fitted)
    battery: float       # V
    status: int          # §5.3 bit field


# --------------------------------------------------------------------------- #
#  Discovery
# --------------------------------------------------------------------------- #
@dataclass
class ProbeResult:
    port: str
    serial: str = ""
    firmware: str = ""


def _parse_idn(idn: str) -> Optional[tuple[str, str]]:
    """('<serial>','<fw>') from an LSA3.1 *IDN?, else None."""
    parts = [p.strip() for p in idn.split(",")]
    if len(parts) < 4 or parts[0].upper() != "FERROVAC" or parts[1] != "LSA3.1":
        return None
    return parts[2], parts[3]


def probe_port(port: str) -> Optional[ProbeResult]:
    """Identify an LSA3.1 on a port; opens, identifies, and *closes*."""
    if not HAVE_SERIAL:
        return None
    lsa = LSA31(port, timeout=1.0)
    try:
        parsed = open_and_identify(lsa, _parse_idn, attempts=2)   # open+warm-up+retried *IDN? (#9)
        if parsed is None:
            return None
        sn, fw = parsed
        return ProbeResult(port=port, serial=sn, firmware=fw)
    except Exception:
        return None
    finally:
        lsa.close()                            # safe no-op if open() itself failed


# --------------------------------------------------------------------------- #
#  ferroDAC device wrapper
# --------------------------------------------------------------------------- #
class LSA31Device(BaseDevice):
    driver = "lsa31"
    discoverable = True

    _cache: dict = {}                # port -> ProbeResult | None
    _active_ports = PORTS_IN_USE     # shared serial arbiter (see keithley6221)
    _cls_lock = SERIAL_LOCK

    def __init__(self, probe: ProbeResult):
        self._probe = probe
        self._port = probe.port
        sources = [
            Source(id="current", name="Pump current", unit="A",
                   modality=Modality.SCALAR, prefer_log=True),
            Source(id="pressure", name="Pressure", unit="mbar",
                   modality=Modality.SCALAR, prefer_log=True),
            Source(id="hv", name="High voltage", unit="V",
                   modality=Modality.SCALAR),
            Source(id="temp", name="Temperature", unit="°C",
                   modality=Modality.SCALAR),
            Source(id="vbat", name="Battery", unit="V",
                   modality=Modality.SCALAR),
        ]
        sinks = [
            # Safety-relevant: OUTP answers synchronously (OK / ERR,4-refused),
            # and a refusal RAISES so the UI never shows a state the instrument
            # declined (the QMS-filament lesson from the 2026-07 audit).
            Sink(id="hv_enable", name="HV output", kind=SinkKind.TOGGLE,
                 value=False),
        ]
        options = [
            # Pump sensitivity makes the pressure channel live (nan until set);
            # text options: numbers are validated instrument-side (ERR,3).
            Option("pump_sens", "Pump sensitivity (mbar/A)", kind="text"),
            Option("filter", "Averaging window (1–16)", kind="text"),
        ]
        super().__init__(
            instance_id=f"lsa31:{probe.serial or probe.port}",
            name="LSA3.1 Ion Pump",
            interface=Interface(kind="usb-cdc",
                                params={"port": probe.port, "baud": BAUD}),
            sources=sources,
            sinks=sinks,
            options=options,
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=2.0,
                             default_hz=1.0, min_hz=0.1, max_hz=5.0),
            primary_source="pressure",
            hardware_id=f"LSA31:{probe.serial or probe.port}",
            model="LSA3.1 Ion-Pump HV Controller",
            manufacturer="Ferrovac",
        )
        self._firmware = probe.firmware or None
        self._lsa: Optional[LSA31] = None
        # one MEAS? serves all five sources of a poll cycle (shared instant —
        # the manual's recommended polling model): cache one parsed line and
        # serve every _read within the same half-cycle from it.
        self._meas: Optional[Meas] = None
        self._meas_at = 0.0

    # -- discovery -------------------------------------------------------------- #
    @classmethod
    def discover(cls):
        if not HAVE_SERIAL:
            return []
        present = {p.device for p in serial.tools.list_ports.comports()}
        with cls._cls_lock:
            for p in [p for p in cls._cache if p not in present]:
                del cls._cache[p]
            to_probe = [p for p in present
                        if p not in cls._cache and p not in cls._active_ports]
        for p in to_probe:
            res = probe_port(p)                       # slow work outside the lock
            with cls._cls_lock:
                if p not in cls._active_ports:
                    cls._cache[p] = res
        with cls._cls_lock:
            results = [r for r in cls._cache.values() if r is not None]
        return [cls(r) for r in results]

    # -- lifecycle -------------------------------------------------------------- #
    def _connect(self) -> None:
        if not HAVE_SERIAL:
            raise RuntimeError("pyserial not available")
        if self._lsa is not None:                 # reconnect without leak
            try:
                self._lsa.close()
            finally:
                self._lsa = None
        lsa = LSA31(self._port)
        try:
            parsed = open_and_identify(lsa, _parse_idn)   # open+warm-up+retried *IDN? (#9)
            if parsed is None:
                raise RuntimeError("not an LSA3.1 on this port")
            self._firmware = parsed[1]
            lsa.clear_status()                    # drop a stale error slot
            # Seed sink/option values from the REAL instrument state (least
            # surprise — the tpg256a/keithley convention). No reset: a running
            # pump keeps pumping; we show the truth.
            self._sink_values["hv_enable"] = lsa.get_output()
            try:
                self._option_values["pump_sens"] = f"{lsa.get_sensitivity():g}"
            except Exception:                     # sensitivity not configured yet
                self._option_values["pump_sens"] = ""
            try:
                self._option_values["filter"] = str(lsa.get_filter())
            except Exception:
                pass
        except Exception:
            lsa.close()
            raise
        self._lsa = lsa
        with type(self)._cls_lock:
            type(self)._active_ports.add(self._port)
            type(self)._cache.pop(self._port, None)

    def _disconnect(self) -> None:
        # Leave the HV state as the operator set it: this instrument's JOB is
        # to keep a portable chamber pumped unattended — an implicit off on
        # disconnect would vent-risk the experiment, not make it safer.
        with self._io_lock:
            if self._lsa is not None:
                self._lsa.close()
                self._lsa = None
            self._meas = None
        with type(self)._cls_lock:
            type(self)._active_ports.discard(self._port)

    # -- data plane -------------------------------------------------------------- #
    def _fresh_meas(self) -> Optional[Meas]:
        """The poll cycle's shared MEAS? line: re-query at most once per
        half-period, so one serial transaction feeds all five sources with a
        single acquisition instant (io_lock held by the caller)."""
        now = time.monotonic()
        max_age = 0.5 / max(self._rate_hz or 1.0, 1e-3)
        if self._meas is None or (now - self._meas_at) > max_age:
            self._meas = self._lsa.meas()
            self._meas_at = now
            on = bool(self._meas.status & ST_HV_ON)   # readback: reflect the REAL HV
            if self._sink_values.get("hv_enable") != on:   # state, even if toggled on
                self._sink_values["hv_enable"] = on        # the front panel (not by us)
                self._mark_sink_dirty()                    # → app re-announces the descriptor
        return self._meas

    def _read(self, source: Source):
        with self._io_lock:
            if self._lsa is None:
                return math.nan, 1
            try:
                m = self._fresh_meas()
            except Exception:
                self._meas = None
                return math.nan, 1
        st = m.status
        if source.id == "current":
            if st & (ST_OVERLOAD | ST_UNDERRANGE):
                return math.nan, 1                # out of range → not a measurement
            return m.current, 0
        if source.id == "pressure":
            if st & (ST_OVERLOAD | ST_UNDERRANGE):
                return math.nan, 1                # derived from an invalid current
            return m.pressure, 0                  # nan until sensitivity is set
        if source.id == "hv":
            return m.voltage, 0
        if source.id == "temp":
            if st & ST_NO_TSENS:
                return math.nan, 1
            return m.temperature, 0
        if source.id == "vbat":
            return m.battery, 0                   # low/critical are warnings, data valid
        return math.nan, 1

    # -- control -------------------------------------------------------------- #
    def _write(self, sink: Sink, value) -> None:
        with self._io_lock:
            if self._lsa is None:
                raise RuntimeError("LSA3.1 link is down")
            if sink.id == "hv_enable":
                # LSA31.output raises on ERR,4 (refused — e.g. battery critical),
                # so a declined enable NEVER silently looks accepted.
                self._lsa.output(bool(value))
            else:
                raise RuntimeError(f"unknown sink {sink.id!r}")

    def _on_option(self, key: str, value) -> None:
        with self._io_lock:
            if self._lsa is None:
                raise RuntimeError("LSA3.1 link is down")
            if key == "pump_sens":
                text = str(value).strip()
                if text:
                    self._lsa.set_sensitivity(float(text))  # ERR,3 raises on bad range
            elif key == "filter":
                text = str(value).strip()
                if text:
                    self._lsa.set_filter(int(text))
