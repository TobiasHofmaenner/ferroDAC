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

# Optional fixed mass-axis offset (amu) to correct a start-of-sweep lead-in,
# tuned against a known peak (e.g. water @ 18). Software-only; default 0.
OFFSET_OPTS = [(-1.0, "−1.0"), (-0.75, "−0.75"), (-0.5, "−0.5"), (-0.25, "−0.25"),
               (0.0, "0"), (0.25, "+0.25"), (0.5, "+0.5"), (0.75, "+0.75"),
               (1.0, "+1.0")]

# C-SEM high voltage: operated ~900–1500 V; clamp generously and let the unit
# reject out-of-range. Exact ceiling + SHV value units are hardware-validated.
SEM_HV_MAX = 2200.0

SCAN_RANGES = [("1-50", "1–50 u"), ("1-100", "1–100 u"), ("1-200", "1–200 u"),
               ("12-50", "12–50 u")]
SPEED_OPTS = [(7, "0.1 s/u"), (8, "0.2 s/u"), (9, "0.5 s/u"),
              (10, "1 s/u"), (11, "2 s/u")]
RES_OPTS = [(0, "Coarse (1/u)"), (1, "Normal (8/u)"), (2, "Fine (64/u)")]
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
            Option("resolution", "Resolution", tuple(RES_OPTS), 1),
            Option("readout", "Readout", tuple(READOUT_OPTS), "peak"),
            Option("mass_offset", "Mass offset", tuple(OFFSET_OPTS), 0.0),
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
        # Measured sweep density (points per amu). The mass of point i is
        # `first + offset + i/_rate` — a fixed rate, not a division by a guessed
        # total — so masses stay put regardless of truncation or dropped points.
        # Learned from completed sweeps; range-independent, so only a resolution
        # or speed change forces a relearn (recalibrate-on-change).
        self._rate = None
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

    def _scan_time(self) -> float:
        width = max(1, self._last - self._first)
        return width * SPEED_S_PER_AMU.get(self._speed, 0.5)

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

    def _read_control_state(self) -> None:
        """Sync the filament / detector / multiplier sinks to the instrument."""
        for sink_id, cmd, parse in (
            ("filament", "FIE", lambda r: r.strip() == "1"),
            ("multiplier", "SEM", lambda r: r.strip() == "1"),
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
        self._write_q.append((sink.id, value))

    def _on_option(self, key: str, value) -> None:
        # Update local scan params immediately (cheap), and flag the poll thread
        # to reprogram the analyzer between scans — no GUI-thread serial I/O.
        self._apply_scan_params()
        if key in ("resolution", "speed"):       # density may change → recalibrate
            self._rate = None
        if key in ("range", "resolution", "speed"):
            self._reconfig_pending = True        # needs a hardware reprogram
        # "readout" / "mass_offset" are software-only: applied on the next frame,
        # no reprogram, no recalibration (rate is unchanged, axis stays valid).

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
        n = max(2, (self._last - self._first) + 1)
        x = np.linspace(self._first, self._last, n)
        return Trace(x, np.full(n, np.nan), x_label="m/z", y_label="Intensity",
                     x_lo=float(self._first), x_hi=float(self._last))

    def _mass_axis(self, n: int) -> np.ndarray:
        """The m/z of each of n sequential points. Uses the measured sweep
        density (points per amu) so the mass of point i is a fixed
        `first + offset + i/rate` — independent of the total count, hence
        immune to truncation and dropped points. Until the rate is learned
        (the first sweep of a config), fall back to assuming this is a complete
        sweep spanning the full range."""
        if self._rate:
            return self._first + self._offset + np.arange(n) / self._rate
        span = self._last - self._first
        return self._first + self._offset + span * np.arange(n) / max(1, n - 1)

    def _make_trace(self, points) -> Trace:
        """Map a (possibly partial) sweep onto its m/z axis. In "peak" readout
        each integer mass becomes the peak intensity in its ±0.5 u window, which
        collapses the dense, log-unfriendly analog sweep into a clean per-mass
        spectrum and rejects single-point noise/valley spikes."""
        y = np.asarray(points, dtype=float)
        x = self._mass_axis(len(y))
        if self._readout == "peak":
            masses = np.arange(self._first, self._last + 1, dtype=float)
            inten = np.full(len(masses), np.nan)
            for k, m in enumerate(masses):
                sel = (x >= m - 0.5) & (x <= m + 0.5)
                if sel.any():
                    inten[k] = np.nanmax(y[sel])
            x, y = masses, inten
        return Trace(x, y, x_label="m/z", y_label="Intensity", y_unit="A",
                     x_lo=float(self._first), x_hi=float(self._last))

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
            self._service_writes()                  # apply queued controls
            t0 = time.monotonic()
            try:
                self._link.query("CRU ,2")          # start a scan
                points = self._drain_scan(source)
            except ProtocolError as exc:
                self._drop_link(str(exc))
                return self._empty(), 1
        if self._reconfig_pending:                  # a setting changed mid-sweep →
            return self._empty(), 1                 # discard this mixed/partial scan
        if len(points) < 2:
            self._last_error = (f"scan returned {len(points)} point(s) — check "
                                "MBH/MDB framing (FERRODAC_QMS_DEBUG=1 for trace)")
            _dbg(f"scan drained {len(points)} points in {time.monotonic()-t0:.1f}s")
            return self._empty(), 1
        # Learn the sweep density (points/amu) from this complete sweep. Taking
        # the max means a one-off truncated sweep can't lower the rate; the
        # longest (full) sweep sets it. Range-independent → reused across ranges.
        width = max(1, self._last - self._first)
        self._rate = max(self._rate or 0.0, (len(points) - 1) / width)
        _dbg(f"scan drained {len(points)} points in {time.monotonic()-t0:.1f}s "
             f"(range {self._first}-{self._last}, speed {self._speed}, "
             f"rate {self._rate:.1f}/amu)")
        return self._make_trace(points), 0

    def _drain_scan(self, source=None) -> list:
        """Read one scan: pull points (MDB) as the buffer (MBH) fills, until the
        scan stops producing or we time out. Point masses come from the measured
        sweep density, not the count. While draining, emit throttled *partial*
        frames so the spectrum fills in live."""
        points: list = []
        idle = 0
        resyncs = 0
        dropped = 0
        emitted = 0
        last_partial = time.monotonic()
        drain_start = time.monotonic()
        # Generous backstop: per-point serial readout lags the sweep, so a full
        # scan can take well over its nominal time. The drain is abortable via
        # `_streaming`, so a long ceiling is safe.
        deadline = drain_start + self._scan_time() * 3.0 + 30.0

        def maybe_emit():
            # Emit a partial "sweep-so-far" a touch faster than the engine's
            # ~50 ms (20 Hz) drain so every repaint has a fresh frame. Gated on a
            # known rate so the live fill places masses correctly; the first
            # sweep of a config (rate unknown) calibrates silently, then streams.
            nonlocal last_partial, emitted
            now = time.monotonic()
            if (source is not None and self._emit is not None and self._rate
                    and points and now - last_partial >= 0.04):
                self._emit(Reading(self.data_id, source.id, time.time(),
                                   self._make_trace(points), 0, partial=True))
                last_partial = now
                emitted += 1

        while self._streaming and time.monotonic() < deadline:
            if self._reconfig_pending:              # option changed → end this
                break                               # sweep so the new config
            self._service_writes()                  # applies promptly; controls
            #                                         apply between chunks too
            maybe_emit()
            try:
                hdr = self._link.query("MBH").split(",")
                avail = int(hdr[3]) if len(hdr) > 3 else 0
            except (ProtocolError, ValueError, IndexError):
                avail = 0
            if avail <= 0:
                idle += 1
                # The analyzer sweeps for ~scan_time; because per-point readout
                # lags, an empty buffer before then just means we briefly caught
                # up mid-sweep — NOT the end. Only conclude the sweep is done once
                # its nominal time has passed and the buffer has then drained.
                swept = time.monotonic() - drain_start >= self._scan_time()
                if points and swept and idle >= 8:
                    break
                time.sleep(0.05)
                continue
            idle = 0
            for _ in range(avail):
                if not self._streaming:
                    break
                try:
                    raw = self._link.query("MDB", flush=False)
                except ProtocolError as exc:
                    # No clean reply at all → genuine desync; flush and re-poll.
                    self._link.resync()
                    resyncs += 1
                    _dbg(f"MDB resync #{resyncs}: {exc}")
                    break
                if _VALUE_RE.match(raw):
                    points.append(float(raw))
                else:
                    # A stray non-value frame jumped the queue (e.g. '3,4,10').
                    # Skip it WITHOUT flushing: the real buffered values are kept
                    # and resurface on the next MBH poll, so the sweep stays
                    # complete and the m/z axis stays aligned.
                    dropped += 1
                    _dbg(f"skipped stray MDB frame {raw!r}")
                maybe_emit()                        # stream during big bursts too
            if resyncs > 20:                        # runaway desync — give up
                _dbg("too many resyncs, aborting drain")
                break
        _dbg(f"{emitted} partial frame(s) emitted, {dropped} stray(s) skipped")
        return points
