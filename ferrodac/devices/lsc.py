"""Ferrovac LSC controller — self-describing UHV load-lock/FIB coordinator.

Two layers, following the lsa31 pattern, so the instrument logic is reusable
outside ferroDAC (cal stations, flashing scripts):

  * :class:`LSC` — a dependency-free (pyserial only) controller speaking the
    LSC line-oriented ASCII protocol (see the firmware repo's
    ``docs/programming-manual.md``). Plain synchronous methods; no ferroDAC/Qt.
    The wire is demuxed by first byte: ``=`` solicited reply, ``#`` streamed
    frame, ``!`` asynchronous event — these interleave, so a pending command's
    ``=`` reply is taken as the next ``=`` line while ``#``/``!`` are surfaced
    via callbacks.
  * :class:`LSCDevice` — the thin ferroDAC ``BaseDevice`` wrapper. It is
    SELF-DESCRIBING: on connect it reads ``DESCRIBE?`` and builds its
    ferroDAC ``Source``/``Sink`` objects from that schema — nothing is
    hard-coded. Device-emitted ``!EVT`` lines become tags (origin=device).

Link: front mini-USB → FTDI FT232R → Due UART0, **1,050,000 baud 8N1, no flow
control, LF terminator** (the measured-clean end-to-end rate — manual §1).

    *IDN?      -> "=Ferrovac,LSC,<serial>,<fw>,<product>"
    DESCRIBE?  -> "=<json>"   (sources[] + sinks[]; read once on connect)
    MEAS?      -> "=<v0>,<v1>,…"   (positional, in sources[] order)
    <sink>:<verb> [arg] -> "=OK" | '=ERR,<code>,"<msg>"'

SAFETY: NEVER open the port at 1200 baud — on this SAM3X/Arduino-Due hardware a
1200-baud open is the bootloader-activation signal and resets the instrument
into firmware-update mode. This driver only ever opens at 1,050,000 (defence in
depth: the constructor rejects 1200 outright, mirroring lsa31 §3.1).
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..core.base import BaseDevice
from ..core.serial_arbiter import PORTS_IN_USE, SERIAL_LOCK
from ..core.tag import ORIGIN_DEVICE, Marker, color_for
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

BAUD = 1050000       # NEVER 1200 — that is the bootloader trigger (manual §1)
TERM = b"\n"

# !EVT kind -> tag severity (manual §7). Unlisted kinds default to "info".
_EVENT_SEVERITY = {
    "leak": "critical",
    "gauge-error": "error",
    "fib-timeout": "warn",
}


class LSCError(Exception):
    """A protocol / link error. ``code`` is the instrument ERR code when the
    device answered ``=ERR,<code>,"<msg>"`` (else None)."""

    def __init__(self, msg: str, code: Optional[int] = None):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------- #
#  Classified device→host lines (demux by first byte, manual §2)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Reply:
    """A solicited ``=`` line; ``text`` is everything after the ``=``."""
    text: str


@dataclass(frozen=True)
class StreamFrame:
    """An unsolicited ``#<ms>,<v0>,…`` frame (STREAM on, manual §5)."""
    ms: int
    values: dict                 # {source_id: typed_value} when described, else {}
    raw: tuple = ()              # the positional value strings, always present


@dataclass(frozen=True)
class Event:
    """An unsolicited ``!EVT,<ms>,<kind>,<detail>`` line (manual §7)."""
    ms: int
    kind: str
    detail: str


# --------------------------------------------------------------------------- #
#  Reusable, dependency-free controller
# --------------------------------------------------------------------------- #
class LSC:
    """Synchronous control of an LSC over its FTDI serial line.

    Pure pyserial — safe to import from non-ferroDAC code::

        with LSC("/dev/ttyUSB0") as lsc:
            print(lsc.idn())
            schema = lsc.describe()
            values = lsc.meas()          # {source_id: typed_value}

    ``#`` stream frames and ``!`` events that arrive while awaiting a ``=`` reply
    are surfaced through the ``on_stream`` / ``on_event`` callbacks (set them on
    the instance or pass in the constructor) so nothing is silently dropped.
    """

    def __init__(self, port: str, baud: int = BAUD, timeout: float = 1.0,
                 on_event: Optional[Callable[[Event], None]] = None,
                 on_stream: Optional[Callable[[StreamFrame], None]] = None):
        if baud == 1200:                       # defence in depth (SAFETY, above)
            raise LSCError("1200 baud is the LSC bootloader trigger")
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.on_event = on_event
        self.on_stream = on_stream
        self._ser = None
        self._schema: Optional[dict] = None
        self._sources: Optional[list] = None   # cached DESCRIBE sources[] (order!)
        self._sinks: Optional[list] = None
        self._desynced = False                 # a timed-out reply may have left the stream
        #                                        offset by one → resync on the next send (§6)

    # -- link ----------------------------------------------------------------- #
    def open(self) -> "LSC":
        if not HAVE_SERIAL:
            raise LSCError("pyserial not available")
        self._ser = serial.Serial(
            self.port, self.baud, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=self.timeout, write_timeout=self.timeout)
        time.sleep(0.15)                       # let the FTDI device settle after open
        try:
            self._ser.reset_input_buffer()     # drop boot noise (only safe moment to)
        except Exception:
            pass
        return self

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> "LSC":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low-level line I/O + demux ------------------------------------------- #
    def _send(self, line: str) -> None:
        if self._ser is None:
            raise LSCError("port not open")
        if self._desynced:                     # a prior timeout may have left a late reply in
            try:                               # the buffer → discard it so THIS command's reply
                self._ser.reset_input_buffer() # is the next '=' we read (deliberate resync, §6)
            except Exception:
                pass
            self._desynced = False
        try:
            self._ser.write(line.encode("ascii") + TERM)
            self._ser.flush()
        except Exception as exc:  # pragma: no cover - hardware I/O
            raise LSCError(f"send {line!r} failed: {exc}") from exc

    def _readline(self) -> Optional[str]:
        """One LF-terminated line, stripped; None on timeout / blank."""
        if self._ser is None:
            raise LSCError("port not open")
        data = self._ser.read_until(TERM)
        if not data:
            return None
        text = data.decode("ascii", "replace").strip("\r\n \t")
        return text or None

    def read_message(self):
        """Read ONE unsolicited device→host line and classify it by first byte
        (manual §2): :class:`Reply` (``=``), :class:`StreamFrame` (``#``),
        :class:`Event` (``!``), or None (timeout / unknown prefix). This is the
        pull API a stream-mode consumer loops on."""
        raw = self._readline()
        if raw is None:
            return None
        c = raw[0]
        if c == "=":
            return Reply(raw[1:])
        if c == "#":
            return self._parse_frame(raw)
        if c == "!":
            return self._parse_event(raw)
        return None                            # bare / unknown line — ignore

    def _await_reply(self) -> str:
        """Return the next ``=`` reply's payload, surfacing any interleaved
        ``#``/``!`` lines through the callbacks (the device serialises a pending
        reply before its ``#``/``!`` — but a stream frame emitted mid-exchange
        can still precede it, so we skip/surface until the ``=`` arrives)."""
        for _ in range(10000):
            raw = self._readline()
            if raw is None:
                self._desynced = True          # a late reply may still be in flight → resync next send
                raise LSCError("no reply (timeout)")
            c = raw[0]
            if c == "=":
                return raw[1:]
            if c == "#":
                self._dispatch(self.on_stream, self._parse_frame(raw))
            elif c == "!":
                self._dispatch(self.on_event, self._parse_event(raw))
            # else: bare/unknown line — skip
        self._desynced = True
        raise LSCError("no reply (too many interleaved frames)")

    @staticmethod
    def _dispatch(cb, msg) -> None:
        if cb is not None:
            try:
                cb(msg)
            except Exception:                  # a callback must never break the link
                pass

    def _parse_frame(self, raw: str) -> StreamFrame:
        fields = raw[1:].split(",")
        try:
            ms = int(fields[0])
        except (IndexError, ValueError):
            ms = 0
        vals = tuple(fields[1:])
        typed: dict = {}
        if self._sources is not None and len(vals) == len(self._sources):
            for src, rawv in zip(self._sources, vals):
                typed[src["id"]] = self._convert(src.get("dtype", "f32"), rawv.strip())
        return StreamFrame(ms=ms, values=typed, raw=vals)

    @staticmethod
    def _parse_event(raw: str) -> Event:
        # "!EVT,<ms>,<kind>,<detail>" — detail is free text (no commas per §7).
        parts = raw[1:].split(",", 3)          # drop the leading '!'
        ms = 0
        if len(parts) > 1:
            try:
                ms = int(parts[1])
            except ValueError:
                ms = 0
        kind = parts[2] if len(parts) > 2 else ""
        detail = parts[3] if len(parts) > 3 else ""
        return Event(ms=ms, kind=kind, detail=detail)

    @staticmethod
    def _parse_err(payload: str) -> tuple[int, str]:
        parts = payload.split(",", 2)          # ERR,<code>,"<message>"
        try:
            code = int(parts[1])
        except (IndexError, ValueError):
            code = -1
        msg = parts[2].strip().strip('"') if len(parts) > 2 else ""
        return code, msg

    @staticmethod
    def _convert(dtype: str, raw: str):
        """A positional MEAS/stream value → its typed Python value (manual §5). TOTAL by
        design: an unparseable field maps to NaN (the invalid-reading sentinel) instead of
        raising, so one garbage value never crashes a MEAS?/stream decode mid-exchange."""
        if raw.lower() == "nan":
            return float("nan")                # invalid / unavailable
        if dtype == "enum":
            return raw                         # the option string (index mapped in _read)
        try:
            if dtype in ("bool", "i32"):
                return int(raw)                # 0 / 1 or a signed int
            return float(raw)                  # f32 (default)
        except (ValueError, TypeError):
            return float("nan")                # garbage field → invalid reading, never a crash

    # -- transactions --------------------------------------------------------- #
    def _query(self, line: str) -> str:
        """Send a command/query; return the ``=`` payload, raising on ``=ERR``."""
        self._send(line)
        payload = self._await_reply()
        if payload == "ERR" or payload.startswith("ERR,"):   # NOT a value like "ERR_LEAK"
            code, msg = self._parse_err(payload)
            raise LSCError(f"query {line!r} error {code}: {msg}", code=code)
        return payload

    # -- identity / schema ---------------------------------------------------- #
    def idn(self) -> str:
        """Raw identity string (after the ``=``); parse with :func:`_parse_idn`."""
        return self._query("*IDN?")

    def describe(self) -> dict:
        """Read the self-describing schema once and cache it (sources[]/sinks[]).
        The sources[] ORDER is the positional key MEAS?/stream frames decode
        against (manual §4/§5)."""
        payload = self._query("DESCRIBE?")
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LSCError(f"malformed DESCRIBE? reply: {payload!r}") from exc
        self._schema = data
        self._sources = list(data.get("sources", []))
        self._sinks = list(data.get("sinks", []))
        return data

    # -- measurement ---------------------------------------------------------- #
    def meas(self) -> dict:
        """All sources in one acquisition, mapped positionally onto the DESCRIBE
        order → ``{source_id: typed_value}`` (f32→float, i32→int, bool→0/1,
        enum→str, invalid→nan). Auto-describes on first use."""
        if self._sources is None:
            self.describe()
        payload = self._query("MEAS?")
        fields = payload.split(",")
        if len(fields) != len(self._sources):
            raise LSCError(
                f"MEAS? arity {len(fields)} != {len(self._sources)} sources")
        out: dict = {}
        for src, raw in zip(self._sources, fields):
            out[src["id"]] = self._convert(src.get("dtype", "f32"), raw.strip())
        return out

    def state(self, sink_id: str) -> str:
        """Read one sink's current state (``<sink>:STATE?`` → ``=<value>``)."""
        return self._query(f"{sink_id}:STATE?")

    # -- control -------------------------------------------------------------- #
    def command(self, sink_id: str, verb: str, arg=None) -> str:
        """Send ``<sink>:<verb> [arg]`` and parse the reply: returns the ``=OK``
        payload, or raises :class:`LSCError` (with ``.code``) on ``=ERR`` — a
        refused command NEVER silently looks accepted (manual §6)."""
        line = f"{sink_id}:{verb}" + ("" if arg is None else f" {arg}")
        self._send(line)
        payload = self._await_reply()
        if payload == "ERR" or payload.startswith("ERR,"):   # NOT a value like "ERR_LEAK"
            code, msg = self._parse_err(payload)
            raise LSCError(f"command {line!r} error {code}: {msg}", code=code)
        return payload

    # -- streaming ------------------------------------------------------------ #
    def stream(self, ms: int) -> str:
        """Start unsolicited ``#`` frames every ``ms`` ms (manual §5)."""
        return self._query(f"STREAM {int(ms)}")

    def stream_off(self) -> str:
        return self._query("STREAM OFF")


# --------------------------------------------------------------------------- #
#  Discovery
# --------------------------------------------------------------------------- #
@dataclass
class ProbeResult:
    port: str
    serial: str = ""
    firmware: str = ""
    product: str = ""
    schema: Optional[dict] = None


def _parse_idn(idn: str) -> Optional[tuple[str, str, str]]:
    """('<serial>','<fw>','<product>') from an LSC *IDN?, else None."""
    parts = [p.strip() for p in idn.split(",")]
    if len(parts) < 5 or parts[0].upper() != "FERROVAC" or parts[1].upper() != "LSC":
        return None
    return parts[2], parts[3], parts[4]


def probe_port(port: str) -> Optional[ProbeResult]:
    """Identify an LSC on a port; opens, identifies, reads the schema, *closes*."""
    if not HAVE_SERIAL:
        return None
    try:
        lsc = LSC(port, timeout=1.0).open()
    except Exception:
        return None
    try:
        parsed = _parse_idn(lsc.idn())
        if parsed is None:
            return None
        sn, fw, product = parsed
        schema = lsc.describe()               # self-describing discovery (§4)
        return ProbeResult(port=port, serial=sn, firmware=fw,
                           product=product, schema=schema)
    except Exception:
        return None
    finally:
        lsc.close()


# --------------------------------------------------------------------------- #
#  Schema → ferroDAC descriptors (the self-describing mapping, manual §4)
# --------------------------------------------------------------------------- #
def _source_from_schema(s: dict) -> Source:
    # Every LSC source carries a FLOAT on the data plane (_read returns a float — an enum
    # becomes its option index), so the ferroDAC dtype is "float" for numeric/enum sources
    # and "bool" for booleans. "str"/"int" are NOT platform dtypes: the router only knows
    # float/bool/trace/… and would silently drop the channel from curation/export/charts.
    dt = s.get("dtype", "f32")
    if dt == "bool":
        modality, pydtype = Modality.STATUS, "bool"
    elif dt == "enum":
        modality, pydtype = Modality.STATUS, "float"   # carried as its option index
    else:                                              # f32 / i32 / unknown → scalar float
        modality, pydtype = Modality.SCALAR, "float"
    return Source(id=s["id"], name=s.get("name", s["id"]), unit=s.get("unit", ""),
                  modality=modality, dtype=pydtype, prefer_log=bool(s.get("log", False)))


def _sink_from_schema(s: dict) -> Sink:
    kind = s.get("kind", "action")
    name = s.get("name", s["id"])
    if kind == "toggle":
        return Sink(id=s["id"], name=name, kind=SinkKind.TOGGLE, value=False)
    if kind == "setpoint":
        params = (Param(name=name, unit=s.get("unit", ""),
                        minimum=s.get("min"), maximum=s.get("max")),)
        return Sink(id=s["id"], name=name, kind=SinkKind.SETPOINT,
                    params=params, value=s.get("min"))
    if kind == "enum":
        options = tuple(s.get("options", ()))
        params = (Param(name=name, options=options),)
        return Sink(id=s["id"], name=name, kind=SinkKind.ENUM,
                    params=params, value=options[0] if options else None)
    return Sink(id=s["id"], name=name, kind=SinkKind.ACTION)   # action / unknown


# --------------------------------------------------------------------------- #
#  ferroDAC device wrapper
# --------------------------------------------------------------------------- #
class LSCDevice(BaseDevice):
    driver = "lsc"
    discoverable = True

    _cache: dict = {}                # port -> ProbeResult | None
    _active_ports = PORTS_IN_USE     # shared serial arbiter (see lsa31)
    _cls_lock = SERIAL_LOCK

    def __init__(self, probe: ProbeResult):
        self._probe = probe
        self._port = probe.port
        schema = probe.schema or {}
        # SELF-DESCRIBING: sources/sinks are built from DESCRIBE, never hardcoded.
        sources = [_source_from_schema(s) for s in schema.get("sources", [])]
        sinks = [_sink_from_schema(s) for s in schema.get("sinks", [])]
        # enum option lists, to map an enum source's option string → an index the
        # scalar data plane can carry (Source has no options field of its own).
        self._enum_options = {s["id"]: tuple(s.get("options", ()))
                              for s in schema.get("sources", [])
                              if s.get("dtype") == "enum"}
        primary = next((s["id"] for s in schema.get("sources", [])
                        if s.get("dtype") == "f32"), None)
        if primary is None and sources:
            primary = sources[0].id
        super().__init__(
            instance_id=f"lsc:{probe.serial or probe.port}",
            name=f"Ferrovac LSC ({probe.product})" if probe.product else "Ferrovac LSC",
            interface=Interface(kind="usb-serial",
                                params={"port": probe.port, "baud": BAUD}),
            sources=sources,
            sinks=sinks,
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=5.0,
                             default_hz=1.0, min_hz=0.1, max_hz=10.0),
            primary_source=primary,
            hardware_id=f"LSC:{probe.serial or probe.port}",
            model=probe.product or "LSC",
            manufacturer="Ferrovac",
        )
        self._firmware = probe.firmware or None
        self._lsc: Optional[LSC] = None
        # One MEAS? feeds every source of a poll cycle (shared instant), cached
        # for at most a half-period — the lsa31 polling model.
        self._meas: Optional[dict] = None
        self._meas_at = 0.0
        # !EVT lines the link surfaced while awaiting replies, drained → tags via the
        # BaseDevice emit_tag() channel (the platform injects the sink; §7.3).
        self._events: list = []

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
        if self._lsc is not None:                 # reconnect without leak
            try:
                self._lsc.close()
            finally:
                self._lsc = None
        lsc = LSC(self._port)
        lsc.on_event = self._events.append        # surface !EVT into the drain queue
        lsc.open()
        try:
            parsed = _parse_idn(lsc.idn())
            if parsed is None:
                raise RuntimeError("not an LSC on this port")
            self._firmware = parsed[1]
            lsc.describe()                        # refresh the positional schema (§4)
            # Seed each writable sink from the REAL instrument state (least
            # surprise — a running pump keeps pumping; we show the truth).
            for sink in self._sinks:
                if sink.kind == SinkKind.ACTION:
                    continue
                try:
                    self._sink_values[sink.id] = self._parse_state(sink, lsc.state(sink.id))
                except Exception:
                    pass                          # sink without a readable state
        except Exception:
            lsc.close()
            raise
        self._lsc = lsc
        self._events.clear()
        with type(self)._cls_lock:
            type(self)._active_ports.add(self._port)
            type(self)._cache.pop(self._port, None)

    def _disconnect(self) -> None:
        # Leave physical outputs as the operator set them — an implicit vent/pump
        # change on disconnect would be an unsafe surprise, not a safe default.
        with self._io_lock:
            if self._lsc is not None:
                self._lsc.close()
                self._lsc = None
            self._meas = None
        with type(self)._cls_lock:
            type(self)._active_ports.discard(self._port)

    @staticmethod
    def _parse_state(sink: Sink, raw: str):
        raw = raw.strip()
        if sink.kind == SinkKind.TOGGLE:
            return raw.lower() in ("1", "on", "open", "true")
        if sink.kind == SinkKind.SETPOINT:
            try:
                return float(raw)
            except ValueError:
                return None
        return raw                                # enum → the option string

    # -- data plane -------------------------------------------------------------- #
    def _fresh_meas(self) -> dict:
        """The poll cycle's shared MEAS? (re-queried at most once per half-period),
        draining any events the exchange surfaced into device-origin tags. The
        io_lock is held by the caller."""
        now = time.monotonic()
        max_age = 0.5 / max(self._rate_hz or 1.0, 1e-3)
        if self._meas is None or (now - self._meas_at) > max_age:
            self._meas = self._lsc.meas()
            self._meas_at = now
            self._drain_events()
        return self._meas

    def _drain_events(self) -> None:
        if not self._events:
            return
        pending, self._events = self._events, []
        for evt in pending:
            self.emit_tag(self._event_to_tag(evt))    # base forwards to the platform sink

    def _event_to_tag(self, evt: Event) -> Marker:
        sev = _EVENT_SEVERITY.get(evt.kind, "info")
        return Marker(
            id=uuid.uuid4().hex,
            t=time.time(),
            kind=evt.kind or "event",
            label=evt.detail or evt.kind,
            color=color_for(evt.kind, sev),
            origin_kind=ORIGIN_DEVICE,
            origin_id=self.data_id,
            scope=f"device:{self.data_id}",
            severity=sev,
            payload={"ms_since_boot": evt.ms, "detail": evt.detail},
            immutable=True,                       # an emitted fact — time is fixed
        )

    def _read(self, source: Source):
        with self._io_lock:
            if self._lsc is None:
                return math.nan, 1
            try:
                m = self._fresh_meas()
            except Exception:
                self._meas = None
                return math.nan, 1
        v = m.get(source.id)
        if v is None:
            return math.nan, 1
        if source.id in self._enum_options:       # enum → index into its options
            #                                       (keyed off the enum map, NOT the dtype,
            #                                        which is "float" so charts/export accept it)
            if isinstance(v, float) and math.isnan(v):
                return math.nan, 1
            opts = self._enum_options[source.id]
            try:
                return float(opts.index(v)), 0
            except ValueError:
                return math.nan, 1
        if isinstance(v, float) and math.isnan(v):
            return math.nan, 1
        return float(v), 0                         # f32/i32/bool all carry as float

    # -- control -------------------------------------------------------------- #
    def _write(self, sink: Sink, value) -> None:
        with self._io_lock:
            if self._lsc is None:
                raise RuntimeError("LSC link is down")
            if sink.kind == SinkKind.TOGGLE:
                self._lsc.command(sink.id, "ON" if value else "OFF")
            elif sink.kind == SinkKind.ACTION:
                self._lsc.command(sink.id, "DO")
            elif sink.kind in (SinkKind.SETPOINT, SinkKind.ENUM):
                self._lsc.command(sink.id, "SET", value)
            else:
                raise RuntimeError(f"unknown sink kind {sink.kind!r}")
