"""Pfeiffer Prisma QMS 200 — quadrupole RGA (mass spectrum) over RS-232.

Same Pfeiffer ACK/ENQ framing as the TPG-256A (manual doc BG 805 204 BE), here
at 19200 baud with a CR-only terminator::

    HOST → "MNEMONIC[,param]\\r"   →   device → <ACK 0x06>
    HOST → <ENQ 0x05>             →   device → "data\\r\\n"

``CMO ,1`` switches it into ASCII/computer-control mode. The QMG command set
(clean-room from the protocol; GPL CINF/PyExpLabSys was reference only):
identify ``SQA`` (4 ⇒ QMS 200); scan setup ``MMO 0``/``MFM``/``MWI``/``MSD``/
``MST``/``MRE`` + range ``AMO``/``ARL``; start ``CRU ,2``; read ``MBH`` header
(field[3] = samples ready) then ``MDB`` per point.

Exposes the scan as a ``trace`` Source (intensity vs m/z) with scan range / speed
/ resolution as device Options. Filament & emission control come in a later
phase. The scan readout (MBH/MDB draining) is faithful to the protocol but the
exact buffering is **to be validated on real hardware** — the m/z axis is
derived from the number of points actually returned, so it is robust to the
unknown points-per-amu mapping.

NB: shares the Pfeiffer link pattern with tpg256a.py; factor a common
``_pfeiffer`` link once a third Pfeiffer driver lands.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import warnings
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from ..core.base import BaseDevice
from ..core.reading import Reading
from ..core.device import (
    Interface,
    Modality,
    Option,
    Param,
    RateControl,
    RateMode,
    Sink,
    SinkKind,
    Source,
    Status,
)
from ..core.trace import Trace

try:
    import serial
    import serial.tools.list_ports
    HAVE_SERIAL = True
except Exception:  # pragma: no cover
    serial = None
    HAVE_SERIAL = False

CR = b"\x0d"
LF = b"\x0a"
ENQ = b"\x05"
ACK = b"\x06"
NAK = b"\x15"
ETX = b"\x03"

PROBE_BAUDRATES = (19200, 9600)
ANALYZER = {0: "QMG 125", 1: "QMG 400", 4: "QMS 200"}

# speed code → seconds per amu (matches the QMG protocol speed_list)
SPEED_S_PER_AMU = {7: 0.1, 8: 0.2, 9: 0.5, 10: 1.0, 11: 2.0}

# Assumed points-per-amu for the very first sweep of a config, before the real
# density is measured from a completed sweep. The X axis is pinned to the scan
# range, so an over/under estimate just clips — it self-corrects on completion.
_DEFAULT_PPA = 32.0

# Fixed mass grid (points/amu) that completed sweeps are resampled onto before
# rolling-averaging — makes the average robust to small per-sweep length
# differences and lets a truncated sweep contribute only over its real range.
_GRID_PPA = 64

# If a sweep reports no new data for this long while still "measuring" (MBH
# running flag never reaching 1), treat it as stalled and restart — bounds the
# rare freeze to a few seconds instead of the full backstop deadline. Points
# arrive every <0.2 s even at the slowest speed, so this won't trip a healthy
# sweep; it only catches a wedged device buffer or a serial desync.
_STALL_TIMEOUT = 4.0

# Within-sweep boxcar smoothing: averages the dense raw points over a window
# (in amu). Kept well under 1 u so noise drops without blurring the peaks.
SMOOTH_AMU = {"Off": 0.0, "0.1 u": 0.1, "0.2 u": 0.2, "0.5 u": 0.5}

# Optional fixed mass-axis offset (amu) to correct a start-of-sweep lead-in,
# tuned against a known peak (e.g. water @ 18). Software-only; default 0.
OFFSET_OPTS = [(-1.0, "−1.0"), (-0.75, "−0.75"), (-0.5, "−0.5"), (-0.25, "−0.25"),
               (0.0, "0"), (0.25, "+0.25"), (0.5, "+0.5"), (0.75, "+0.75"),
               (1.0, "+1.0")]

# Display noise floor: intensities below this — including the negative excursions
# of the measurement noise — are clamped UP to it, so the log plot shows a clean
# baseline instead of gaps where the signal dips below zero. "Off" disables it.
FLOOR_OPTS = [(0.0, "Off"), (1e-14, "1e-14 A"), (1e-13, "1e-13 A"),
              (1e-12, "1e-12 A"), (1e-11, "1e-11 A")]

# C-SEM high voltage: operated ~900–1500 V; clamp generously and let the unit
# reject out-of-range. Exact ceiling + SHV value units are hardware-validated.
SEM_HV_MAX = 2200.0

SCAN_RANGES = [("1-50", "1–50 u"), ("1-100", "1–100 u"), ("1-200", "1–200 u"),
               ("12-50", "12–50 u")]
SPEED_OPTS = [(7, "0.1 s/u"), (8, "0.2 s/u"), (9, "0.5 s/u"),
              (10, "1 s/u"), (11, "2 s/u")]
# Resolution code (MST) → measurement points per amu. Confirmed on the real
# Prisma: MST=1 gives 32 points/amu (1568 pts over 1–50 u), matching the QMG
# step encoding (0→1/64 u, 1→1/32 u, 2→1/16 u, 3→1/8 u). Higher density = better
# resolution but a slower scan. The unit may clamp a requested code, so we read
# the effective value back rather than trust what we sent.
RES_OPTS = [(0, "Highest (64/u)"), (1, "High (32/u)"),
            (2, "Normal (16/u)"), (3, "Low (8/u)")]
PPA_FROM_MST = {0: 64, 1: 32, 2: 16, 3: 8}
# How the raw analog sweep is reduced to a spectrum. "peak" = one point per
# integer mass (the peak intensity in that ±0.5 u window) — the clean RGA bar
# view, robust to noise/dropped points. "analog" = the full raw sweep.
READOUT_OPTS = [("peak", "Peaks per mass"), ("analog", "Full analog scan")]


class ProtocolError(Exception):
    pass


# Opt-in raw-protocol trace for hardware bring-up: run with FERRODAC_QMS_DEBUG=1
# to dump every mnemonic + its raw reply to stderr (CRU/MBH/MDB scan draining).
_DEBUG = bool(os.environ.get("FERRODAC_QMS_DEBUG"))


def _dbg(msg: str) -> None:
    if _DEBUG:
        print(f"[qms200] {msg}", file=sys.stderr, flush=True)


# A measurement value is a single signed float in scientific notation, e.g.
# '+1.00300E-11'. Anything else on the MDB channel is a stray/partial frame.
_VALUE_RE = re.compile(r"^[+-]?\d+\.\d+E[+-]?\d+$")


# --------------------------------------------------------------------------- #
#  Serial link (ACK/ENQ, hardened)
# --------------------------------------------------------------------------- #
class _Link:
    def __init__(self, ser, port: str, baud: int):
        self.ser = ser
        self.port = port
        self.baud = baud

    def _send(self, mnemonic: str, flush: bool = True) -> None:
        # `flush` clears stale RX before a command. It is left ON for config /
        # control / MBH, but turned OFF for the rapid per-point MDB drain: at
        # speed the reset chops bytes out of an in-flight value and desyncs the
        # framing (seen as half-values like b'8258E-12').
        if flush:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
        self.ser.write(mnemonic.encode("ascii") + CR)
        self.ser.flush()
        resp = self.ser.read_until(expected=LF).lstrip(b"\r\n\x00")
        if not resp:
            raise ProtocolError(f"no ACK to {mnemonic!r}")
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

    def query(self, mnemonic: str, attempts: int = 3, flush: bool = True) -> str:
        last = None
        for _ in range(attempts):
            try:
                self._send(mnemonic, flush=flush)
                resp = self._enquire()
                _dbg(f"{mnemonic!r} -> {resp!r}")
                return resp
            except ProtocolError as exc:
                last = exc
                _dbg(f"{mnemonic!r} !! {exc}")
                self.resync()
                time.sleep(0.05)
        raise ProtocolError(f"{mnemonic} failed: {last}")

    def resync(self) -> None:
        """Drop any unread/partial frame so the next query re-aligns."""
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


def _open_serial(port: str, baud: int, timeout: float = 0.8):
    return serial.Serial(
        port, baud, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=timeout, write_timeout=timeout)


@dataclass
class ProbeResult:
    port: str
    baud: int
    analyzer: int
    serial: str = ""


def list_ports() -> list:
    return [p.device for p in serial.tools.list_ports.comports()] if HAVE_SERIAL else []


def probe_port(port: str, baudrates=PROBE_BAUDRATES):
    """Identify a QMG analyzer on a port (CMO ,1 → SQA); open/identify/close."""
    for baud in baudrates:
        try:
            ser = _open_serial(port, baud)
        except Exception:
            return None
        try:
            time.sleep(0.15)
            ser.reset_input_buffer()
            link = _Link(ser, port, baud)
            for _ in range(3):
                try:
                    link.query("CMO ,1")            # ASCII / computer control
                    sqa = link.query("SQA").strip()
                    if sqa in ("0", "1", "4"):       # a QMG-family analyzer
                        ser.close()
                        return ProbeResult(port, baud, int(sqa))
                except ProtocolError:
                    pass
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            if ser.is_open:
                ser.close()
    return None


# --------------------------------------------------------------------------- #
#  Device
# --------------------------------------------------------------------------- #
class QMS200Device(BaseDevice):
    driver = "qms200"
    discoverable = True

    _cache: dict = {}
    _active_ports: set = set()
    _cls_lock = threading.Lock()

    def __init__(self, probe: ProbeResult):
        self._port = probe.port
        self._baud = probe.baud
        self._atype = probe.analyzer
        model = ANALYZER.get(probe.analyzer, "Pfeiffer QMG")
        options = [
            Option("range", "Scan range", tuple(SCAN_RANGES), "1-50"),
            Option("speed", "Scan speed", tuple(SPEED_OPTS), 9),
            Option("resolution", "Resolution", tuple(RES_OPTS), 2),
            Option("readout", "Readout", tuple(READOUT_OPTS), "peak"),
            Option("mass_offset", "Mass offset", tuple(OFFSET_OPTS), 0.0),
            Option("floor", "Noise floor", tuple(FLOOR_OPTS), 1e-13),
        ]
        super().__init__(
            instance_id=f"qms:{probe.port}",
            name=model,
            interface=Interface(kind="rs232",
                                params={"port": probe.port, "baud": probe.baud}),
            sources=[Source(id="spectrum", name="Mass spectrum", unit="",
                            modality=Modality.WAVEFORM, dtype="trace",
                            prefer_log=True)],
            sinks=[
                Sink(id="filament", name="Filament", kind=SinkKind.TOGGLE,
                     value=False),
                Sink(id="detector", name="Detector", kind=SinkKind.ENUM,
                     params=(Param("mode", "str", options=("Faraday", "SEM")),),
                     value="Faraday"),
                Sink(id="multiplier", name="Multiplier (SEM HV)",
                     kind=SinkKind.TOGGLE, value=False),
                Sink(id="sem_voltage", name="SEM voltage", kind=SinkKind.SETPOINT,
                     params=(Param("v", "float", "V",
                                   minimum=0.0, maximum=SEM_HV_MAX),),
                     value=900.0),
                Sink(id="smoothing", name="Smoothing", kind=SinkKind.ENUM,
                     params=(Param("w", "str", options=tuple(SMOOTH_AMU)),),
                     value="Off"),
                Sink(id="average", name="Average sweeps", kind=SinkKind.ENUM,
                     params=(Param("n", "str",
                                   options=("1", "2", "4", "8", "16", "32")),),
                     value="1"),
                # Route a total-pressure gauge here (or set manually): each sweep
                # is scaled so its integral equals this pressure, turning the
                # arbitrary ion currents into real partial pressures. 0 = off.
                Sink(id="ref_pressure", name="Normalize to pressure",
                     kind=SinkKind.SETPOINT,
                     params=(Param("p", "float", "mbar", minimum=0.0),),
                     value=0.0),
            ],
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=1.0,
                             default_hz=0.5, min_hz=0.02, max_hz=2.0),
            primary_source="spectrum",
            hardware_id=f"QMS200:{probe.serial or probe.port}",
            model=f"Pfeiffer {model}",
            options=options,
        )
        self._link = None
        self._io_lock = threading.Lock()
        self._last_reopen = 0.0
        # Control writes / option changes are queued from the GUI thread and
        # applied by the poll thread at a safe serial boundary, so the UI never
        # blocks waiting for a scan to finish. deque ops are atomic in CPython.
        self._write_q: deque = deque()
        self._reconfig_pending = False
        # The actual scan parameters READ BACK from the instrument (never the
        # values we *sent* — the unit may clamp them). A completed sweep of N
        # points is `linspace(actual_first, actual_last, N)`: the count is
        # authoritative, so masses can't drift. _expected_n (the full point
        # count) is learned from a completed sweep to place live partial frames;
        # the first sweep estimates it from the read-back resolution.
        self._actual_first = 1.0
        self._actual_last = 50.0
        self._actual_mst = 1
        self._expected_n = None
        # Rolling sweep-average (driver-level, noise drops as √N). _avg_n is set
        # from the "Average sweeps" sink; _avg_buf holds recent sweeps resampled
        # onto a fixed mass grid. Only the poll thread touches _avg_buf.
        self._avg_n = 1
        self._avg_buf: list = []
        self._smooth_amu = 0.0       # within-sweep boxcar window (amu); 0 = off
        # Total-pressure normalisation: scale each sweep so its integral equals
        # _ref_pressure (written via the ref_pressure sink, e.g. routed from a
        # gauge). _norm_scale carries the last scale onto live partial frames.
        self._ref_pressure = 0.0
        self._norm_scale = None
        self._apply_scan_params()

    # -- discovery -----------------------------------------------------------
    @classmethod
    def discover(cls):
        if not HAVE_SERIAL:
            return []
        serials = {p.device: (p.serial_number or "")
                   for p in serial.tools.list_ports.comports()}
        present = set(serials)
        with cls._cls_lock:
            for p in [p for p in cls._cache if p not in present]:
                del cls._cache[p]
            to_probe = [p for p in present
                        if p not in cls._cache and p not in cls._active_ports]
        for p in to_probe:
            res = probe_port(p)
            if res is not None:
                res.serial = serials.get(p, "")
            with cls._cls_lock:
                if p not in cls._active_ports:
                    cls._cache[p] = res
        with cls._cls_lock:
            results = [r for r in cls._cache.values() if r is not None]
        return [cls(r) for r in results]

    # -- scan configuration --------------------------------------------------
    def _apply_scan_params(self) -> None:
        rng = str(self._option_values.get("range", "1-50"))
        try:
            first, last = (int(x) for x in rng.split("-"))
        except ValueError:
            first, last = 1, 50
        self._first, self._last = first, last
        self._speed = int(self._option_values.get("speed", 9))
        self._mst = int(self._option_values.get("resolution", 1))
        self._readout = str(self._option_values.get("readout", "peak"))
        self._offset = float(self._option_values.get("mass_offset", 0.0))
        self._floor = float(self._option_values.get("floor", 1e-13))

    def _configure_scan(self) -> None:
        """Program the analyzer for a mass scan (idempotent)."""
        link = self._link
        width = max(1, self._last - self._first)
        for cmd in (
            "CMO ,1",            # ASCII control
            "CYM ,0",            # single (not multi) channel mode
            "SMC ,0",            # channel 0
            "MMO ,0",            # mass-scan mode
            "MRE ,1",            # resolve peak
            f"MST ,{self._mst}",
            f"MSD ,{self._speed}",
            f"MFM ,{self._first}",
            f"MWI ,{width}",
            "AMO ,1",            # auto-range with lower limit
            "ARL ,-11",
        ):
            link.query(cmd)
        self._read_scan_params()

    def _read_scan_params(self) -> None:
        """Read the *effective* scan parameters back from the instrument and use
        them (not what we sent) to build the mass axis. The unit clamps codes —
        e.g. a requested MST 0 comes back as 1. `MFM` reads as a float ('1.00'),
        `MWI` as a signed int ('+49'). Falls back to the sent values on error."""
        link = self._link
        try:
            self._actual_first = float(link.query("MFM"))
            self._actual_last = self._actual_first + int(link.query("MWI"))
            self._actual_mst = int(link.query("MST"))
        except (ProtocolError, ValueError, IndexError) as exc:
            self._actual_first = float(self._first)
            self._actual_last = float(self._last)
            self._actual_mst = self._mst
            _dbg(f"param read-back failed ({exc}); using sent settings")
        # seed the expected full-sweep count from the read-back resolution; a
        # completed sweep then refines it.
        ppa = PPA_FROM_MST.get(self._actual_mst, 32)
        self._expected_n = int(round((self._actual_last - self._actual_first) * ppa)) + 1
        _dbg(f"read-back: first={self._actual_first} last={self._actual_last} "
             f"mst={self._actual_mst} → ~{self._expected_n} pts")

    def _read_control_state(self) -> None:
        """Sync the filament / detector / multiplier sinks to the instrument."""
        for sink_id, cmd, parse in (
            ("filament", "FIE", lambda r: r.strip() == "1"),
            # SEM reads back as 'on,actualHV' e.g. '1,1248' — the on/off is field 0.
            ("multiplier", "SEM", lambda r: r.split(",")[0].strip() == "1"),
            ("detector", "SDT", lambda r: "SEM" if (r.strip() and int(r) > 0)
             else "Faraday"),
            ("sem_voltage", "SHV", lambda r: float(r.split(",")[0].strip())),
        ):
            try:
                self._sink_values[sink_id] = parse(self._link.query(cmd))
            except (ProtocolError, ValueError):
                pass

    # -- control sinks (filament / detector / SEM) ---------------------------
    def _write(self, sink, value) -> None:
        """Queue a control change; the poll thread applies it at the next safe
        serial boundary (no GUI-thread blocking on the scan). Only fires when
        the user writes the sink; the ion source is never auto-enabled."""
        if sink.id == "average":                 # software-only, no serial
            try:
                self._avg_n = max(1, int(value))
            except (TypeError, ValueError):
                self._avg_n = 1
            return
        if sink.id == "smoothing":               # software-only, no serial
            self._smooth_amu = SMOOTH_AMU.get(value, 0.0)
            return
        if sink.id == "ref_pressure":            # software-only; routable from a gauge
            try:
                self._ref_pressure = max(0.0, float(value))
            except (TypeError, ValueError):
                self._ref_pressure = 0.0
            return
        self._write_q.append((sink.id, value))

    def _on_option(self, key: str, value) -> None:
        # Update local scan params immediately (cheap), and flag the poll thread
        # to reprogram the analyzer between scans — no GUI-thread serial I/O.
        self._apply_scan_params()
        if key in ("resolution", "speed"):       # density may change → re-estimate
            self._expected_n = None              # from the read-back on reprogram
        if key in ("range", "resolution", "speed", "mass_offset"):
            self._avg_buf = []                   # grid moved → drop the average
            #                                      (floor/readout don't move it)
        if key in ("range", "resolution", "speed"):
            self._reconfig_pending = True        # → reprogram + read params back
        # "readout" / "mass_offset" are software-only: applied on the next frame,
        # no reprogram (the read-back axis stays valid).

    def _send_control(self, sink_id: str, value) -> None:
        """The actual serial send for a control sink (poll thread, IO lock held)."""
        if sink_id == "filament":
            self._link.query("FIE ,1" if value else "FIE ,0")
        elif sink_id == "multiplier":
            self._link.query("SEM ,1" if value else "SEM ,0")
        elif sink_id == "detector":
            self._link.query("SDT ,1" if value == "SEM" else "SDT ,0")
        elif sink_id == "sem_voltage":
            # Stage the multiplier HV; only physically applied while the
            # multiplier (SEM) is on, which the user controls separately.
            self._link.query(f"SHV ,{int(round(value))}")

    def _service_writes(self) -> None:
        """Drain queued control writes onto the link. Poll thread, IO lock held."""
        while self._write_q and self._link is not None:
            sink_id, value = self._write_q.popleft()
            try:
                self._send_control(sink_id, value)
            except ProtocolError as exc:
                self._drop_link(str(exc))
                break

    # -- lifecycle -----------------------------------------------------------
    def _connect(self) -> None:
        if not HAVE_SERIAL:
            raise RuntimeError("pyserial not available")
        self._open_link()
        try:
            self._firmware = self._link.query("SQA")  # analyzer-type echo
        except ProtocolError:
            self._firmware = None
        self._configure_scan()
        self._read_control_state()
        with type(self)._cls_lock:
            type(self)._active_ports.add(self._port)
            type(self)._cache.pop(self._port, None)

    def _disconnect(self) -> None:
        self._write_q.clear()
        with self._io_lock:
            if self._link is not None:
                try:
                    self._link.query("FIE ,0")   # leave the filament off
                except Exception:
                    pass
                self._link.close()
                self._link = None
        with type(self)._cls_lock:
            type(self)._active_ports.discard(self._port)

    def _open_link(self) -> None:
        self._link = _Link(_open_serial(self._port, self._baud), self._port, self._baud)

    def _reopen(self) -> bool:
        now = time.monotonic()
        if now - self._last_reopen < 3.0:
            return False
        self._last_reopen = now
        try:
            self._open_link()
            self._configure_scan()
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def _drop_link(self, msg: str) -> None:
        self._last_error = msg
        if self._link is not None:
            self._link.close()
            self._link = None

    # -- data plane ----------------------------------------------------------
    def _empty(self):
        lo, hi = self._actual_first, self._actual_last
        n = max(2, int(round(hi - lo)) + 1)
        x = np.linspace(lo, hi, n)
        return Trace(x, np.full(n, np.nan), x_label="m/z", y_label="Intensity",
                     x_lo=float(lo), x_hi=float(hi))

    def _ppa(self, n_full: int) -> float:
        """Points per amu implied by a full sweep of `n_full` points."""
        span = self._actual_last - self._actual_first
        return (n_full - 1) / span if span > 0 and n_full > 1 else _DEFAULT_PPA

    def _axis(self, n: int, complete: bool) -> np.ndarray:
        """The m/z of `n` sequential points, from the READ-BACK first/last mass.
        A complete sweep spans [first, last] exactly (linspace over its real
        count — the count is authoritative, so masses can't drift). A partial
        fills from the left at the full-sweep spacing so points land correctly
        before the sweep finishes."""
        first = self._actual_first + self._offset
        last = self._actual_last + self._offset
        if complete:
            return np.linspace(first, last, max(2, n))
        n_full = max(self._expected_n or n, 2)
        step = (last - first) / (n_full - 1) if n_full > 1 else 1.0
        return first + np.arange(n) * step

    def _reduce(self, x, y, sigma=None) -> Trace:
        """Apply the readout reduction to a (mass, intensity) signal, carrying an
        optional per-point noise array. In "peak" each integer mass becomes the
        peak intensity in its ±0.5 u window (and that point's noise), which
        collapses the dense, log-unfriendly analog sweep into a clean per-mass
        spectrum and rejects single-point noise/valley spikes."""
        if self._readout == "peak":
            masses = np.arange(self._first, self._last + 1, dtype=float)
            inten = np.full(len(masses), np.nan)
            sig = np.full(len(masses), np.nan) if sigma is not None else None
            for k, m in enumerate(masses):
                sel = (x >= m - 0.5) & (x <= m + 0.5)
                vals = y[sel]
                ok = np.isfinite(vals)
                if ok.any():
                    j = np.flatnonzero(sel)[ok][np.argmax(vals[ok])]
                    inten[k] = y[j]
                    if sigma is not None:
                        sig[k] = sigma[j]
            x, y, sigma = masses, inten, sig
        if self._floor > 0:                      # clamp sub-floor / negative noise
            y = np.where(y < self._floor, self._floor, y)   # NaN (no data) preserved
        return Trace(x, y, x_label="m/z", y_label="Intensity", y_unit="A",
                     x_lo=float(self._first), x_hi=float(self._last), sigma=sigma)

    def _smooth(self, y: np.ndarray, ppa: float) -> np.ndarray:
        """Within-sweep boxcar: average the dense raw points over a window of
        `smooth_amu` (converted to points via the sweep density `ppa`). Reduces
        per-point noise; kept under 1 u so the peaks aren't blurred."""
        w = int(round(self._smooth_amu * ppa))
        if w < 2 or w >= len(y):
            return y
        return np.convolve(y, np.ones(w) / w, mode="same")

    def _normalize(self, trace: Trace, recompute: bool) -> Trace:
        """Scale the spectrum so its integral equals the reference pressure, so
        peaks read as real partial pressures. The scale is recomputed from a
        completed sweep's full integral and reused (`_norm_scale`) on partial
        frames, which span only part of the spectrum. 0 reference = off."""
        if self._ref_pressure <= 0:
            return trace
        if recompute:
            total = float(np.nansum(np.clip(trace.y, 0.0, None)))
            if total > 0:
                self._norm_scale = self._ref_pressure / total
        if self._norm_scale:
            trace.y = trace.y * self._norm_scale
            if trace.sigma is not None:
                trace.sigma = trace.sigma * self._norm_scale
            trace.y_unit = "mbar"
        return trace

    def _make_trace(self, points) -> Trace:
        """Reduce one raw sweep (smoothed, no averaging) — for live partials."""
        n = len(points)
        y = self._smooth(np.asarray(points, dtype=float),
                         self._ppa(self._expected_n or n))
        trace = self._reduce(self._axis(n, complete=False), y)
        return self._normalize(trace, recompute=False)

    def _make_avg_trace(self, points) -> Trace:
        """Reduce a completed sweep: within-sweep smoothing, then rolling-average
        the last N sweeps (noise ∝ 1/√N). Sweeps are resampled onto a fixed mass
        grid so small per-sweep length differences don't matter."""
        n = len(points)
        y = self._smooth(np.asarray(points, dtype=float), self._ppa(n))
        if self._avg_n <= 1:
            self._avg_buf = []
            trace = self._reduce(self._axis(n, complete=True), y)
        else:
            x = self._axis(n, complete=True)
            lo, hi = self._actual_first + self._offset, self._actual_last + self._offset
            grid = np.linspace(lo, hi, max(2, int(round((hi - lo) * _GRID_PPA)) + 1))
            gy = np.interp(grid, x, y, left=np.nan, right=np.nan)
            buf = self._avg_buf
            if buf and len(buf[0]) != len(gy):   # grid changed → restart
                buf = []
            buf.append(gy)
            del buf[:-self._avg_n]                # keep only the last N
            self._avg_buf = buf
            stack = np.vstack(buf)
            with warnings.catch_warnings():       # edge grid points can be all-NaN
                warnings.simplefilter("ignore", RuntimeWarning)
                avg = np.nanmean(stack, axis=0)
                # measured per-mass noise of the average = sweep std / sqrt(N)
                sd = (np.nanstd(stack, axis=0) / np.sqrt(len(buf))
                      if len(buf) >= 2 else None)
            trace = self._reduce(grid, avg, sigma=sd)
        return self._normalize(trace, recompute=True)

    def _read(self, source):
        with self._io_lock:
            if self._link is None and not self._reopen():
                return self._empty(), 1
            if self._reconfig_pending:              # apply option changes here,
                self._reconfig_pending = False      # between scans (coherent axis)
                try:
                    self._configure_scan()
                except ProtocolError as exc:
                    self._drop_link(str(exc))
                    return self._empty(), 1
            self._service_writes()                  # apply queued controls (between sweeps)
            t0 = time.monotonic()
            try:
                self._link.query("CRU ,2")          # start a sweep
                points, complete = self._drain_scan(source)
            except ProtocolError as exc:
                self._drop_link(str(exc))
                return self._empty(), 1
        if self._reconfig_pending:                  # a setting changed mid-sweep →
            return self._empty(), 1                 # discard this mixed/partial scan
        if not complete or len(points) < 2:
            self._last_error = (f"sweep ended incomplete ({len(points)} point(s)) "
                                "— FERRODAC_QMS_DEBUG=1 for the MBH/MDB trace")
            _dbg(f"sweep incomplete: {len(points)} pts in {time.monotonic()-t0:.1f}s")
            return self._empty(), 1
        # Authoritative full count → density for the next sweep's live partials.
        self._expected_n = len(points)
        _dbg(f"sweep complete: {len(points)} pts in {time.monotonic()-t0:.1f}s "
             f"({self._actual_first:.0f}-{self._actual_last:.0f} u, "
             f"{self._ppa(len(points)):.0f}/amu, avg {self._avg_n})")
        return self._make_avg_trace(points), 0

    def _drain_scan(self, source=None):
        """Read one sweep **deterministically**: pull points (MDB) as the buffer
        header (MBH) reports them available, and conclude the sweep is complete
        only when MBH **field [0] flips to 1** (the instrument's measurement-done
        flag) and the buffer has drained. Crucially, an empty buffer alone is
        NOT the end — mid-sweep it momentarily empties, which the old timing
        heuristic mistook for completion and truncated the sweep. Returns
        ``(points, complete)`` and emits throttled partial frames while draining.
        """
        points: list = []
        dropped = emitted = resyncs = 0
        last_partial = drain_start = last_progress = time.monotonic()
        # Backstop only — normal completion is the field-[0] flag, not a timer.
        # Sized to the expected point count (per-point serial readout dominates).
        deadline = drain_start + max(60.0, (self._expected_n or 400) * 0.08 + 30.0)

        def maybe_emit():
            nonlocal last_partial, emitted
            now = time.monotonic()
            if (source is not None and self._emit is not None
                    and points and now - last_partial >= 0.04):
                self._emit(Reading(self.data_id, source.id, time.time(),
                                   self._make_trace(points), 0, partial=True))
                last_partial = now
                emitted += 1

        while self._streaming and time.monotonic() < deadline:
            if self._reconfig_pending:              # option changed → end the sweep
                break
            n0 = len(points)
            try:
                hdr = self._link.query("MBH").split(",")
                running = int(hdr[0])               # 1 = measurement complete
                avail = int(hdr[3]) if len(hdr) > 3 else 0
            except (ProtocolError, ValueError, IndexError):
                running, avail = 0, 0
            for _ in range(avail):
                if not self._streaming:
                    break
                try:
                    raw = self._link.query("MDB", flush=False)
                except ProtocolError as exc:
                    self._link.resync()             # genuine desync → re-poll MBH
                    resyncs += 1
                    _dbg(f"MDB resync #{resyncs}: {exc}")
                    break
                if _VALUE_RE.match(raw):
                    points.append(float(raw))
                else:
                    # A stray non-value frame jumped the queue (e.g. '3,4,10').
                    # Skip it without flushing so the real values stay aligned.
                    dropped += 1
                    _dbg(f"skipped stray MDB frame {raw!r}")
                maybe_emit()
            now = time.monotonic()
            if len(points) > n0:                    # made progress this poll
                last_progress = now
            if running == 1 and avail == 0:         # ← deterministic completion
                _dbg(f"complete: {len(points)} pts, {emitted} partial(s), "
                     f"{dropped} stray(s)")
                return points, True
            if avail == 0:
                # Still measuring (running != 1) but no data. A brief gap is
                # normal; a long one means the sweep stalled (device buffer wedged
                # or a desync) — abort so the poll loop starts a fresh sweep
                # promptly instead of waiting out the full backstop deadline.
                if points and now - last_progress > _STALL_TIMEOUT:
                    _dbg(f"sweep STALLED at {len(points)} pts (no data for "
                         f"{now - last_progress:.0f}s) — restarting")
                    return points, False
                time.sleep(0.03)
            if resyncs > 30:                        # runaway desync — bail
                _dbg("too many resyncs, aborting drain")
                break
            maybe_emit()
        return points, False                        # aborted / timed out
