"""Ferrovac LSC controller — self-describing UHV load-lock/FIB coordinator.

Two layers, following the lsa31 pattern, so the instrument logic is reusable
outside ferroDAC (cal stations, flashing scripts):

  * :class:`LSC` — a dependency-free (pyserial only) controller speaking the
    LSC line-oriented ASCII protocol (see the firmware repo's
    ``docs/programming-manual.md``). Plain synchronous methods; no ferroDAC/Qt.
    The wire is demuxed by first byte: ``=`` solicited reply, ``#`` streamed
    frame, ``!`` asynchronous event, ``?`` an operator prompt (``?ASK`` raised /
    ``?DONE`` resolved) — these interleave, so a pending command's ``=`` reply is
    taken as the next ``=`` line while ``#``/``!``/``?`` are surfaced via callbacks.
  * :class:`LSCDevice` — the thin ferroDAC ``BaseDevice`` wrapper. It is
    SELF-DESCRIBING: on connect it reads ``DESCRIBE?`` and builds its
    ferroDAC ``Source``/``Sink`` objects from that schema — nothing is
    hard-coded. Device-emitted ``!EVT`` lines become tags (origin=device), and
    ``?ASK`` prompt lines become device→app→device requests via ``BaseDevice.ask``.

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
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..core.base import BaseDevice
from ..core.interaction import (
    ACKNOWLEDGE, CHOICE, CONFIRM, KINDS, SEVERITIES, STAY, Prompt)
from ..core.serial_arbiter import PORTS_IN_USE, SERIAL_LOCK
from ..core.serial_connect import open_and_identify
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

# A HARD read error = the device is gone (unplug / FTDI re-enumeration / controller reset),
# distinct from a quiet-line read timeout. The reader stops + flags link-down on these so the
# supervisor can auto-reconnect instead of spinning a dead FD forever (issue #10).
_HARD_ERRORS = (serial.SerialException, OSError) if HAVE_SERIAL else (OSError,)

BAUD = 1050000       # NEVER 1200 — that is the bootloader trigger (manual §1)
TERM = b"\n"
LINK_DEAD_AFTER = 3   # consecutive MEAS? transport failures before the link is declared dead (#10)
PROBE_RETRY_S = 30.0  # re-probe a port whose probe FAILED after this cooldown, so a transient
#                       glitch can't stick a live device as undiscoverable forever (issue #6)

# !EVT kind -> tag severity (manual §7). Unlisted kinds default to "info".
_EVENT_SEVERITY = {
    "leak": "critical",
    "gauge-error": "error",
    "fib-timeout": "warn",
}

# The transition slug's trailing action -> its human past-tense/state word, so a
# tag label names the TRANSITION, not just the actuator ("Vent Valve closed", not
# "Vent Valve"). Parsed generically off the kind slug (e.g. "vent-close" -> "close");
# an unlisted suffix falls back to the raw kind (issue #8). No per-actuator table.
_ACTION = {"open": "opened", "close": "closed", "on": "on", "off": "off"}


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


@dataclass(frozen=True)
class Ask:
    """A device-raised operator prompt (manual §8):
    ``?ASK,<id>,<kind>,<sev>,<timeout_ms>,<nopts>,<opt0>[,<opt1>...],<question>``.
    ``id`` is a small firmware int the RESPOND/DONE correlate on; ``options`` is the
    ordered answer set (RESPOND carries the 0-based index into it)."""
    id: int
    kind: str                    # confirm|choice|text|acknowledge (firmware slug)
    severity: str                # info|warn|critical
    timeout_ms: int              # device-side deadline, 0 = none
    options: tuple               # ordered answer options (RESPOND indexes into this)
    question: str                # the full question (may contain commas — it is the tail)


@dataclass(frozen=True)
class Done:
    """A device-side prompt resolution (manual §8): ``?DONE,<id>,<answer>,<by>`` —
    the front panel or another host answered first (first-responder-wins)."""
    id: int
    answer: str
    by: str


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

    ``#`` stream frames, ``!`` events and ``?`` prompts (``?ASK``/``?DONE``) that
    arrive while awaiting a ``=`` reply are surfaced through the ``on_stream`` /
    ``on_event`` / ``on_ask`` / ``on_done`` callbacks (set them on the instance or
    pass in the constructor) so nothing is silently dropped.
    """

    def __init__(self, port: str, baud: int = BAUD, timeout: float = 1.0,
                 on_event: Optional[Callable[[Event], None]] = None,
                 on_stream: Optional[Callable[[StreamFrame], None]] = None,
                 on_ask: Optional[Callable[["Ask"], None]] = None,
                 on_done: Optional[Callable[["Done"], None]] = None):
        if baud == 1200:                       # defence in depth (SAFETY, above)
            raise LSCError("1200 baud is the LSC bootloader trigger")
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.on_event = on_event
        self.on_stream = on_stream
        self.on_ask = on_ask                   # '?ASK' — a device-raised operator prompt (§8)
        self.on_done = on_done                 # '?DONE' — that prompt resolved (panel/other host)
        self._ser = None
        self._schema: Optional[dict] = None
        self._sources: Optional[list] = None   # cached DESCRIBE sources[] (order!)
        self._sinks: Optional[list] = None
        self._desynced = False                 # a timed-out reply may have left the stream
        #                                        offset by one → resync on the next send (§6)
        # Optional background reader (start_reader): decouples '!'/'#' delivery from polling
        # (issue #4). When running it OWNS port reads — '=' replies go to _reply_q, '#'/'!'
        # are dispatched to the callbacks the moment they arrive — and commands read their
        # reply off the queue under _txn_lock (one reply ↔ one command).
        self._reply_q: "queue.Queue" = queue.Queue()
        self._txn_lock = threading.Lock()
        self._reader: "Optional[threading.Thread]" = None
        self._reader_stop = threading.Event()
        self._resync_needed = False            # a reader-mode timeout → hard-resync next txn
        self._link_down = threading.Event()    # the reader hit a HARD error → device gone (#10)

    # -- link ----------------------------------------------------------------- #
    def open(self) -> "LSC":
        if not HAVE_SERIAL:
            raise LSCError("pyserial not available")
        kw = dict(bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                  stopbits=serial.STOPBITS_ONE, timeout=self.timeout,
                  write_timeout=self.timeout)
        if sys.platform.startswith(("linux", "darwin")):
            kw["exclusive"] = True         # POSIX TIOCEXCL: a stray reader/screen/cat on the
            #                                port can't silently corrupt a live link (issue #6)
        self._ser = serial.Serial(self.port, self.baud, **kw)
        self._link_down.clear()                # a fresh open → the link is up (#10)
        time.sleep(0.15)                       # let the FTDI device settle after open
        try:
            self._ser.reset_input_buffer()     # drop boot noise (only safe moment to)
        except Exception:
            pass
        return self

    @property
    def link_down(self) -> bool:
        """The reader thread hit a HARD I/O error (the device is gone) — the device layer's
        supervisor watches this to auto-reconnect instead of polling a dead FD (issue #10)."""
        return self._link_down.is_set()

    def close(self) -> None:
        self.stop_reader()                     # stop reading before the port goes away
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> "LSC":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- background reader (decouples '!'/'#' delivery from polling, issue #4) - #
    def start_reader(self) -> None:
        """Own the port from a background thread so '!' events and '#' frames are
        dispatched the moment they arrive — independent of whether anything is polling.
        Commands then read their '=' reply from a queue under _txn_lock. Idempotent.
        Do NOT mix with the synchronous read_message() pull API — the reader owns reads."""
        if self._reader is not None:
            return
        try:                                   # clean start: drop any residue — a late reply
            if self._ser is not None:          # from a timed-out handshake read, or in-transit
                self._ser.reset_input_buffer() # stale bytes on a hard resync (safe: no reader yet,
        except Exception:                      # on_event already dispatched handshake events)
            pass
        self._drain_reply_q()
        self._desynced = False
        self._resync_needed = False
        self._reader_stop.clear()
        self._reader = threading.Thread(target=self._reader_loop,
                                        name=f"lsc-reader-{self.port}", daemon=True)
        self._reader.start()

    def stop_reader(self) -> None:
        self._reader_stop.set()
        t, self._reader = self._reader, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set():
            try:
                raw = self._readline()             # read_until returns as soon as a line lands
            except _HARD_ERRORS:                   # the device is GONE (unplug / re-enumeration)
                if not self._reader_stop.is_set(): # (a deliberate close raises too — don't flag it)
                    self._link_down.set()          # signal the supervisor + STOP spinning a dead
                break                              # FD, instead of retrying it forever (issue #10)
            except Exception:                      # a generic/unexpected blip → brief backoff
                if self._reader_stop.is_set():
                    break
                self._reader_stop.wait(0.05)       # still stop-responsive
                continue
            if raw is None:                        # no full line this cycle
                self._reader_stop.wait(0.02)       # yield (not busy) + check stop
                continue
            c = raw[0]
            if c == "=":
                self._reply_q.put(raw[1:])         # a command's reply → the waiting transaction
            elif c == "#":
                self._dispatch(self.on_stream, self._parse_frame(raw))
            elif c == "!":
                self._dispatch(self.on_event, self._parse_event(raw))
            elif c == "?":
                self._dispatch_prompt(raw)         # ?ASK/?DONE → on_ask/on_done (manual §8)
            # else: bare/unknown line — skip

    def _drain_reply_q(self) -> None:
        try:
            while True:
                self._reply_q.get_nowait()         # discard stale '=' from a prior timeout
        except queue.Empty:
            pass

    # -- low-level line I/O + demux ------------------------------------------- #
    def _send(self, line: str) -> None:
        if self._ser is None:
            raise LSCError("port not open")
        if self._desynced and self._reader is None:   # synchronous mode only: a prior timeout
            try:                               # may have left a late reply in the buffer → discard
                self._ser.reset_input_buffer() # it so THIS command's reply is the next '=' (§6).
            except Exception:                  # (reader mode resyncs by draining _reply_q instead,
                pass                           #  so buffered '!' events are never lost.)
            self._desynced = False
        try:
            self._ser.write(line.encode("ascii") + TERM)
            self._ser.flush()
        except Exception as exc:  # pragma: no cover - hardware I/O
            raise LSCError(f"send {line!r} failed: {exc}") from exc

    def _readline(self) -> Optional[str]:
        """One LF-terminated line, stripped; None on timeout / blank."""
        ser = self._ser                        # snapshot: check-then-use is atomic vs close()
        if ser is None:
            raise LSCError("port not open")
        data = ser.read_until(TERM)
        if not data:
            return None
        text = data.decode("ascii", "replace").strip("\r\n \t")
        return text or None

    def read_message(self):
        """Read ONE unsolicited device→host line and classify it by first byte
        (manual §2): :class:`Reply` (``=``), :class:`StreamFrame` (``#``),
        :class:`Event` (``!``), :class:`Ask`/:class:`Done` (``?``), or None
        (timeout / unknown prefix). This is the pull API a stream-mode consumer
        loops on."""
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
        if c == "?":
            return self._parse_prompt(raw)     # Ask / Done / None (manual §8)
        return None                            # bare / unknown line — ignore

    def _await_reply(self) -> str:
        """Return the next ``=`` reply's payload. In READER mode the reader thread owns
        reads and has already dispatched any interleaved ``#``/``!`` — the reply waits on
        ``_reply_q``. In synchronous mode we read directly, surfacing interleaved
        ``#``/``!`` through the callbacks until the ``=`` arrives."""
        if self._reader is not None:
            try:
                return self._reply_q.get(timeout=max(self.timeout * 2, 0.3))
            except queue.Empty:
                self._resync_needed = True     # a late reply may be in-transit → hard-resync next txn,
                raise LSCError("no reply (timeout)")   # so it can't be misread as the next command's
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
            elif c == "?":
                self._dispatch_prompt(raw)         # ?ASK/?DONE → on_ask/on_done (manual §8)
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

    def _dispatch_prompt(self, raw: str) -> None:
        """Classify a ``?`` prompt line and route it: ``?ASK`` → ``on_ask``,
        ``?DONE`` → ``on_done`` (manual §8). An unknown ``?`` verb is skipped."""
        msg = self._parse_prompt(raw)
        if isinstance(msg, Ask):
            self._dispatch(self.on_ask, msg)
        elif isinstance(msg, Done):
            self._dispatch(self.on_done, msg)

    @staticmethod
    def _parse_prompt(raw: str):
        """Classify a ``?`` device→host prompt line (manual §8) into :class:`Ask` /
        :class:`Done`, or None for an unknown/malformed ``?`` verb (ignored, like a
        bare line — a bad prompt never crashes the demux)."""
        tok, _, rest = raw[1:].partition(",")   # drop the leading '?'; split verb ↔ body
        tok = tok.upper()
        if tok == "ASK":
            return LSC._parse_ask(rest)
        if tok == "DONE":
            return LSC._parse_done(rest)
        return None

    @staticmethod
    def _parse_ask(rest: str) -> "Optional[Ask]":
        # "<id>,<kind>,<sev>,<timeout_ms>,<nopts>,<opt0>[,<opt1>...],<question>".
        # The question is the tail (may contain commas); nopts locates where it starts.
        fields = rest.split(",")
        if len(fields) < 6:
            return None                          # malformed — ignore rather than raise
        try:
            fw_id = int(fields[0])
        except ValueError:
            return None
        kind = fields[1].strip()
        sev = fields[2].strip()
        try:
            timeout_ms = int(fields[3])
        except ValueError:
            timeout_ms = 0
        try:
            nopts = max(0, int(fields[4]))
        except ValueError:
            nopts = 0
        options = tuple(f.strip() for f in fields[5:5 + nopts])
        question = ",".join(fields[5 + nopts:]).strip()   # tail — commas preserved
        return Ask(id=fw_id, kind=kind, severity=sev, timeout_ms=timeout_ms,
                   options=options, question=question)

    @staticmethod
    def _parse_done(rest: str) -> "Optional[Done]":
        # "<id>,<answer>,<by>" — answer/by are simple tokens; keep any stray commas
        # in `by` out of the way with a bounded split.
        fields = rest.split(",", 2)
        try:
            fw_id = int(fields[0])
        except (IndexError, ValueError):
            return None
        answer = fields[1].strip() if len(fields) > 1 else ""
        by = fields[2].strip() if len(fields) > 2 else ""
        return Done(id=fw_id, answer=answer, by=by)

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
    def _transact(self, line: str) -> str:
        """Send a line and return its ``=`` reply payload, serialized so exactly one
        reply maps to one command (the poll and control paths share the port)."""
        with self._txn_lock:
            if self._reader is not None:
                if self._resync_needed:        # a prior timeout may have left a late reply in-transit
                    self.stop_reader()         # → HARD resync: flush the serial buffer AND the queue by
                    self.start_reader()        #   restarting the reader, so it can't be misread as ours
                else:
                    self._drain_reply_q()      # normal: just drop any already-queued stale '='
            self._send(line)
            return self._await_reply()

    def _query(self, line: str) -> str:
        """Send a command/query; return the ``=`` payload, raising on ``=ERR``."""
        payload = self._transact(line)
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
        payload = self._transact(line)
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

    # -- operator prompts (device→app→device requests, manual §8) ------------- #
    def respond(self, fw_id: int, index: int) -> str:
        """Answer an OPEN device prompt: ``RESPOND <id> <index>`` — ``index`` is the
        0-based position in the ``?ASK`` options (manual §8). First-responder-wins across
        the front panel and every host, so the firmware harmlessly ignores a RESPOND for
        an already-resolved id. Raises :class:`LSCError` on an ``=ERR`` reply."""
        line = f"RESPOND {int(fw_id)} {int(index)}"
        payload = self._transact(line)
        if payload == "ERR" or payload.startswith("ERR,"):
            code, msg = self._parse_err(payload)
            raise LSCError(f"respond {line!r} error {code}: {msg}", code=code)
        return payload

    def prompts_query(self) -> str:
        """``PROMPTS?`` — ask the device to re-announce every OPEN prompt as a fresh
        ``?ASK`` (manual §8), so a late/reconnecting host re-learns a mid-modal request.
        The store is idempotent-by-id, so a re-announced prompt de-dups."""
        return self._query("PROMPTS?")


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
    lsc = LSC(port, timeout=1.0)
    try:
        parsed = open_and_identify(lsc, _parse_idn, attempts=2)   # open+warm-up+retried *IDN? (#9)
        if parsed is None:
            return None
        sn, fw, product = parsed
        lsc._desynced = True                  # resync before DESCRIBE? — a sacrificial *IDN?'s
        schema = lsc.describe()               # late reply mustn't corrupt it (§4 discovery)
        return ProbeResult(port=port, serial=sn, firmware=fw,
                           product=product, schema=schema)
    except Exception:
        return None
    finally:
        lsc.close()                           # safe no-op if open() itself failed


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
    reconnectable = True             # a physical serial link — the platform supervisor auto-
    #                                  reconnects a dropped one (issue #10); its _connect/
    #                                  _disconnect reopen the port cleanly (no destructive side).

    _cache: dict = {}                # port -> ProbeResult | None
    _probe_cooldown: dict = {}       # port -> earliest monotonic re-probe time (failed probes)
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
        self._xport_fails = 0            # consecutive MEAS? transport FAILURES (link-dead signal
        #                                 for the reconnect supervisor — a raise, never a value #10)
        # !EVT lines become device-origin tags via BaseDevice.emit_tag() (§7.3). Delivery
        # is driven by the LSC controller's background reader thread — the moment an event
        # arrives, NOT gated on a MEAS?/poll cycle (issue #4).
        # ?ASK lines become device→app→device prompts via BaseDevice.ask(). fw_id -> (Prompt,
        # options): the firmware knows only the small int id (not the Prompt's uuid), so we keep
        # the map to translate a ?DONE and to send RESPOND <fw_id> <index> back. It OUTLIVES a
        # reconnect (same LSCDevice) so PROMPTS? re-announces the SAME Prompt uuid → the store
        # de-dups instead of duplicating an open request. Touched by the reader thread (?ASK/
        # ?DONE) and the GUI thread (on_response) → guarded by _prompt_lock.
        self._open_prompts: dict = {}
        self._prompt_lock = threading.Lock()

    # -- discovery -------------------------------------------------------------- #
    @classmethod
    def discover(cls):
        if not HAVE_SERIAL:
            return []
        present = {p.device for p in serial.tools.list_ports.comports()}
        now = time.monotonic()
        with cls._cls_lock:
            for p in [p for p in cls._cache if p not in present]:
                del cls._cache[p]
            for p in [p for p in cls._active_ports if p not in present]:
                cls._active_ports.discard(p)          # #6: a vanished port can't be "in use"
                cls._probe_cooldown.pop(p, None)
            # probe a port not yet cached, OR one whose cached FAILURE has cooled down — so a
            # transient probe glitch doesn't leave a present device undiscoverable forever (#6).
            to_probe = [p for p in present
                        if p not in cls._active_ports
                        and (p not in cls._cache
                             or (cls._cache[p] is None
                                 and now >= cls._probe_cooldown.get(p, 0.0)))]
        for p in to_probe:
            res = probe_port(p)                       # slow work outside the lock
            with cls._cls_lock:
                if p not in cls._active_ports:
                    cls._cache[p] = res
                    if res is None:
                        cls._probe_cooldown[p] = now + PROBE_RETRY_S   # back off, then retry
                    else:
                        cls._probe_cooldown.pop(p, None)
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
        lsc.on_event = self._on_event             # capture events even DURING the handshake
        #                                           (the tag sink is already wired by the manager;
        #                                            the reader is off, so idn() dispatches !EVT
        #                                            synchronously — start_reader() is after identify)
        lsc.on_ask = self._on_ask                 # a modal already open on connect surfaces too
        lsc.on_done = self._on_done               # (both fire on the reader thread once it starts)
        try:
            parsed = open_and_identify(lsc, _parse_idn)   # open+warm-up+retried *IDN? (#9)
            if parsed is None:
                raise RuntimeError("not an LSC on this port")
            self._firmware = parsed[1]
            lsc._desynced = True                  # resync before DESCRIBE? so a sacrificial *IDN?'s
            lsc.describe()                        # late reply can't corrupt the positional schema (§4)
            # Seed each writable sink from the REAL instrument state (least
            # surprise — a running pump keeps pumping; we show the truth).
            for sink in self._sinks:
                if sink.kind == SinkKind.ACTION:
                    continue
                try:
                    self._sink_values[sink.id] = self._parse_state(sink, lsc.state(sink.id))
                except Exception:
                    pass                          # sink without a readable state
            # Background reader: events (a leak, a front-panel valve press) reach the
            # timeline in real time even while the device sits idle — no MEAS?/poll needed
            # (issue #4). Inside the try so a failure here still closes the port.
            lsc.start_reader()
            # Re-announce any modal already open on the front panel so a mid-modal RECONNECT
            # re-surfaces it (the reader delivers the ?ASK; the store de-dups by uuid). Tolerate
            # firmware without PROMPTS? — an =ERR here must not fail an otherwise-good connect.
            try:
                lsc.prompts_query()
            except LSCError:
                pass
        except Exception:
            lsc.close()
            raise
        self._lsc = lsc
        self._xport_fails = 0                      # fresh link (also resets after auto-reconnect)
        with type(self)._cls_lock:
            type(self)._active_ports.add(self._port)
            type(self)._cache.pop(self._port, None)

    def _disconnect(self) -> None:
        # Leave physical outputs as the operator set them — an implicit vent/pump
        # change on disconnect would be an unsafe surprise, not a safe default.
        try:
            with self._io_lock:
                try:
                    if self._lsc is not None:
                        self._lsc.close()
                except Exception:             # a corrupted/dropped port can error on close —
                    pass                      # swallow it so the arbiter is STILL released (#6)
                finally:
                    self._lsc = None
                    self._meas = None
        finally:
            with type(self)._cls_lock:        # ALWAYS free the port, on any teardown path (#6)
                type(self)._active_ports.discard(self._port)

    # -- reconnect supervisor hooks (issue #10) ------------------------------- #
    def _link_healthy(self):
        """Report the link DOWN (a transport failure, never a value) so the platform
        supervisor auto-reconnects: the controller flagged a hard I/O error (device gone),
        or MEAS? has failed LINK_DEAD_AFTER times in a row (a lingering-handle quiet drop),
        or we're already torn down. Otherwise alive."""
        lsc = self._lsc
        if lsc is None or lsc.link_down or self._xport_fails >= LINK_DEAD_AFTER:
            return False
        return True

    def _port_present(self) -> bool:
        """Don't hammer a reconnect at a physically-removed device — only retry while the
        serial node still exists (issue #10)."""
        if not HAVE_SERIAL:
            return False
        try:
            return self._port in {p.device for p in serial.tools.list_ports.comports()}
        except Exception:                     # noqa: BLE001 — enumeration hiccup → assume present
            return True

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
        """The poll cycle's shared MEAS? (re-queried at most once per half-period). The
        io_lock is held by the caller. (Event tags are delivered by the reader thread, not
        here — see _on_event / issue #4.)"""
        now = time.monotonic()
        max_age = 0.5 / max(self._rate_hz or 1.0, 1e-3)
        if self._meas is None or (now - self._meas_at) > max_age:
            try:
                self._meas = self._lsc.meas()
            except Exception:
                self._xport_fails += 1        # a transport FAILURE (a raise) — link-dead signal
                raise                          # (the supervisor keys on this, never on a value #10)
            self._meas_at = now
            self._xport_fails = 0             # a good acquisition → the link is alive
        return self._meas

    def _on_event(self, evt: Event) -> None:
        """A !EVT surfaced by the controller's reader thread → a device-origin tag, in
        real time and independent of polling (issue #4). Runs on the reader thread;
        emit_tag's injected sink marshals to the GUI thread."""
        self.emit_tag(self._event_to_tag(evt))

    def _event_to_tag(self, evt: Event) -> Marker:
        sev = _EVENT_SEVERITY.get(evt.kind, "info")
        # Compose the label from BOTH halves of the !EVT line so the timeline names
        # the TRANSITION, not just the actuator: evt.detail is the actuator name
        # ("Vent Valve") and evt.kind is the transition slug ("vent-close"). Parse
        # the action suffix off the slug generically (issue #8). Degrade to the old
        # `detail or kind` when either half is missing, so it's never worse than before.
        if evt.detail and evt.kind:
            action = _ACTION.get(evt.kind.rsplit("-", 1)[-1], evt.kind)
            label = f"{evt.detail} {action}"      # "Vent Valve closed", "Scroll Pump on"
        else:
            label = evt.detail or evt.kind
        return Marker(
            id=uuid.uuid4().hex,
            t=time.time(),
            kind=evt.kind or "event",
            label=label,
            color=color_for(evt.kind, sev),
            origin_kind=ORIGIN_DEVICE,
            origin_id=self.data_id,
            scope=f"device:{self.data_id}",
            severity=sev,
            payload={"ms_since_boot": evt.ms, "detail": evt.detail},
            immutable=True,                       # an emitted fact — time is fixed
        )

    # -- operator prompts (?ASK/?DONE → BaseDevice.ask, mirror of _on_event) --- #
    def _on_ask(self, ask: Ask) -> None:
        """A ?ASK surfaced by the reader thread → a device→app→device Prompt via
        BaseDevice.ask(), in real time (mirrors _on_event / issue #4). Keeps fw_id →
        (Prompt, options) so ?DONE can be translated and the answer mapped back to the
        firmware option index. A re-announced fw_id (PROMPTS?/reconnect) re-uses the SAME
        Prompt so the store de-dups. Runs on the reader thread; ask's injected sink
        marshals to the GUI thread."""
        with self._prompt_lock:
            existing = self._open_prompts.get(ask.id)
            if existing is not None:
                prompt = existing[0]              # re-announce → SAME uuid, store de-dups
            else:
                prompt = self._ask_to_prompt(ask)
                self._open_prompts[ask.id] = (prompt, tuple(ask.options))
        # on_response runs on the GUI thread when the operator answers (first-responder-wins);
        # bind the firmware id so the callback maps the answer to the option index + RESPONDs.
        self.ask(prompt, on_response=lambda answer, fw_id=ask.id: self._respond(fw_id, answer))

    def _on_done(self, done: Done) -> None:
        """A ?DONE — the front panel or another host already answered (first-responder-wins).
        Drop the fw_id so our on_response becomes a GUARDED NO-OP (a late operator answer can
        never RESPOND again → never double-answered), AND withdraw the prompt from the app's
        inbox so a modal answered ON THE DEVICE doesn't linger as pending (via the platform
        withdraw channel, BaseDevice.withdraw_prompt)."""
        with self._prompt_lock:
            entry = self._open_prompts.pop(done.id, None)
        if entry is not None:
            self.withdraw_prompt(entry[0].id)      # entry = (Prompt, options) → retire by uuid

    def _ask_to_prompt(self, ask: Ask) -> Prompt:
        """Map a firmware ?ASK onto a Qt-free core.interaction.Prompt. The device owns the
        timeout (its ?DONE by=timeout closes the request), so on_timeout=STAY: the host never
        independently auto-answers (the §safety model — a host timeout is not a 2nd authority)."""
        sev = ask.severity if ask.severity in SEVERITIES else "info"
        timeout = ask.timeout_ms / 1000.0 if ask.timeout_ms and ask.timeout_ms > 0 else None
        return Prompt(
            device_id=self.data_id,
            question=ask.question,
            kind=self._prompt_kind(ask.kind),
            options=list(ask.options),
            severity=sev,
            timeout=timeout,
            on_timeout=STAY,
        )

    @staticmethod
    def _prompt_kind(fw_kind: str) -> str:
        """Firmware kind slug → a core.interaction kind. An unknown slug degrades to
        ACKNOWLEDGE (a single OK), so a new firmware kind still renders (never a crash)."""
        k = (fw_kind or "").strip().lower()
        return k if k in KINDS else ACKNOWLEDGE

    @staticmethod
    def _answer_to_index(answer, options, kind) -> int:
        """The operator's answer → the 0-based firmware option INDEX RESPOND carries. For a
        confirm the firmware advertises opt0=OptionFalse (negative), opt1=OptionTrue (affirmative)
        and decodes index!=0 as true, so the affirmative is index 1: True→1, False→0. A choice maps
        by option string; acknowledge/text collapse to the single/first option. TOTAL — a surprising
        answer maps to 0 rather than raising inside the on_response callback."""
        if kind == CONFIRM:
            return 1 if answer else 0
        if kind == CHOICE:
            if isinstance(answer, str) and answer in options:
                return options.index(answer)
            if isinstance(answer, bool):          # bool is an int subclass — guard before int
                return 0
            if isinstance(answer, int) and 0 <= answer < max(len(options), 1):
                return answer                     # already an index
            return 0
        return 0                                   # acknowledge / text / unknown

    def _respond(self, fw_id: int, answer) -> None:
        """The Prompt's on_response, invoked once on the GUI thread by the store (first-
        responder-wins): pop the fw_id (so a racing ?DONE / second responder can't double-
        answer), map the answer to the option index, and send RESPOND <fw_id> <index>. Raises
        if the link is down / the device refuses, so the store records the ack as failed."""
        with self._prompt_lock:
            entry = self._open_prompts.pop(fw_id, None)
        if entry is None:
            return                                 # already resolved (?DONE / another surface)
        prompt, options = entry
        index = self._answer_to_index(answer, options, prompt.kind)
        with self._io_lock:
            if self._lsc is None:
                raise RuntimeError("LSC link is down")
            self._lsc.respond(fw_id, index)

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
