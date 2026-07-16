"""Ferrovac LSC controller driver: the dependency-free LSC controller plus the
self-describing BaseDevice wrapper, exercised against a fake serial port that
emulates the instrument's line protocol (1,050,000/LF, demux by first byte) —
no hardware. Protocol reference: lsc-firmware docs/programming-manual.md (DRAFT)."""
import json
import math
import threading
import time

import pytest

pytest.importorskip("serial")

import ferrodac.devices.lsc as mod  # noqa: E402
from ferrodac.devices.lsc import (  # noqa: E402
    LSC,
    LSCDevice,
    LSCError,
    Event,
    ProbeResult,
    StreamFrame,
    _parse_idn,
    _sink_from_schema,
    probe_port,
)
from ferrodac.core.device import Modality, SinkKind  # noqa: E402
from ferrodac.core.tag import ORIGIN_DEVICE  # noqa: E402

IDN = "Ferrovac,LSC,0001,demo-0.1,LSCIAKTHFIHELIOS"

# The self-describing schema (manual §4), the wire contract the driver builds on.
SCHEMA = {
    "idn": IDN,
    "sources": [
        {"id": "p_loadlock", "name": "Load Lock Gauge", "unit": "mbar",
         "dtype": "f32", "log": True},
        {"id": "gv_fib", "name": "FIB Gate Valve", "unit": "", "dtype": "bool"},
        {"id": "pump", "name": "Scroll Pump", "unit": "", "dtype": "bool"},
        {"id": "fib_state", "name": "FIB Request", "unit": "", "dtype": "enum",
         "options": ["idle", "requested", "accepted", "timeout"]},
    ],
    "sinks": [
        {"id": "vent", "name": "Vent Valve", "kind": "toggle"},
        {"id": "rough", "name": "Roughing Line Valve", "kind": "toggle"},
        {"id": "gv_fib", "name": "FIB Gate Valve", "kind": "toggle"},
        {"id": "pump", "name": "Scroll Pump", "kind": "toggle"},
        {"id": "fib_xfer", "name": "FIB Transfer", "kind": "action"},
    ],
}


class FakeSerial:
    """Minimal LSC emulator: line-oriented ASCII, LF-terminated, demux by first
    byte. Replies are buffered; ``inject`` pushes an unsolicited ``#``/``!`` line
    so tests can drive the interleave/demux path."""

    KNOWN_SINKS = {"vent", "rough", "gv_fib", "pump", "fib_xfer"}

    def __init__(self, *a, idn=IDN, **k):
        self.schema = json.loads(json.dumps(SCHEMA))     # deep copy
        self.order = [s["id"] for s in self.schema["sources"]]
        self.meas = {"p_loadlock": 8.4e-07, "gv_fib": 1, "pump": 1,
                     "fib_state": "idle"}
        self.sink_state = {"vent": 0, "rough": 0, "gv_fib": 1, "pump": 1}
        self._idn = idn
        self._buf = bytearray()
        self.meas_count = 0
        self.streaming = False
        self.refuse = False                              # toggle → ERR,4
        self.is_open = True
        self.commands = []                               # every line the host sent (for asserts)
        self.drop_next_reply = False                     # simulate a device that misses one reply
        self.idn_fail = 0                                # first N *IDN? return ERR (first-connect #9)
        self._lock = threading.Lock()                    # _buf is touched by the reader thread too

    # -- pyserial surface the controller touches --
    def reset_input_buffer(self):
        with self._lock:
            self._buf = bytearray()

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def write(self, data: bytes):
        line = data.decode().strip()
        resps = self._respond(line)                      # mutates state, not _buf
        with self._lock:
            for resp in resps:
                self._buf += (resp + "\n").encode()
        return len(data)

    def read_until(self, term=b"\n"):
        with self._lock:
            idx = self._buf.find(term)
            if idx == -1:
                return b""                               # timeout (no full line)
            end = idx + len(term)
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out

    def inject(self, text: str):
        """Push an unsolicited device→host line (event/stream frame)."""
        with self._lock:
            self._buf += (text + "\n").encode()

    # -- the instrument --
    @staticmethod
    def _fmt(v):
        if isinstance(v, str):
            return v
        if isinstance(v, float):
            return "nan" if math.isnan(v) else f"{v:g}"
        return str(v)                                    # int / bool → 0/1

    def _respond(self, line: str):
        self.commands.append(line)                       # record for write-path assertions
        if self.drop_next_reply:                         # device misses this reply → host times out
            self.drop_next_reply = False
            return []
        up = line.upper()
        if up == "*IDN?":
            if self.idn_fail > 0:                        # first-connect transient / leftover (#9)
                self.idn_fail -= 1
                return ['=ERR,1,"Unknown command"']
            return ["=" + self._idn]
        if up == "DESCRIBE?":
            return ["=" + json.dumps(self.schema)]
        if up == "MEAS?":
            self.meas_count += 1
            return ["=" + ",".join(self._fmt(self.meas[k]) for k in self.order)]
        if up == "STREAM OFF":
            self.streaming = False
            return ["=OK"]
        if up.startswith("STREAM "):
            self.streaming = True
            return ["=OK"]
        if ":" in line:
            left, right = line.split(":", 1)
            sink = left.strip().lower()
            body = right.strip()
            if body.upper() == "STATE?":
                if sink in self.sink_state:
                    return ["=" + str(self.sink_state[sink])]
                return ['=ERR,2,"No such state"']
            if sink not in self.KNOWN_SINKS:
                return ['=ERR,2,"Unknown sink"']
            parts = body.split(None, 1)
            verb = parts[0].upper()
            if verb in ("ON", "OPEN", "OFF", "CLOSE"):
                if self.refuse:
                    return ['=ERR,4,"Command refused"']
                self.sink_state[sink] = 1 if verb in ("ON", "OPEN") else 0
                return ["=OK"]
            if verb in ("DO", "SET"):
                return ["=OK"]
            return ['=ERR,3,"Unknown verb"']
        return ['=ERR,1,"Unknown command"']


@pytest.fixture
def fake(monkeypatch):
    """Route LSC.open at a FakeSerial; yields the shared instance and, on teardown,
    disconnects any LSCDevice built via _device() so its reader thread stops (and the
    shared _active_ports/_cache class state is left clean between tests)."""
    fs = FakeSerial()
    fs.devices = []                                      # LSCDevices to tear down
    monkeypatch.setattr(mod.serial, "Serial", lambda *a, **k: fs)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    import ferrodac.core.serial_connect as sc           # the handshake helper's own settle sleep
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    yield fs
    for d in fs.devices:
        try:
            d._disconnect()
        except Exception:
            pass


def _spin(cond, timeout=2.0, interval=0.005):
    """Wait for cond() to become truthy (the reader thread delivers asynchronously).
    Uses a real Event.wait — time.sleep is monkeypatched to a no-op in these tests."""
    ev = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        ev.wait(interval)
    return bool(cond())


# -- controller: link + identity --------------------------------------------- #

def test_baud_default_and_1200_rejected():
    assert LSC("/dev/ttyUSB0").baud == 1050000
    with pytest.raises(LSCError, match="bootloader"):
        LSC("/dev/ttyUSB0", baud=1200)


def test_idn_and_parse(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        assert lsc.idn() == IDN
    assert _parse_idn(IDN) == ("0001", "demo-0.1", "LSCIAKTHFIHELIOS")
    assert _parse_idn("Ferrovac,LSA3.1,x,y,z") is None    # wrong model
    assert _parse_idn("Ferrovac,LSC,0001,demo-0.1") is None  # too few fields
    assert _parse_idn("garbage") is None


# -- controller: DESCRIBE / MEAS --------------------------------------------- #

def test_describe_returns_sources_and_sinks_in_order(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        d = lsc.describe()
    assert [s["id"] for s in d["sources"]] == \
        ["p_loadlock", "gv_fib", "pump", "fib_state"]
    assert [s["id"] for s in d["sinks"]] == \
        ["vent", "rough", "gv_fib", "pump", "fib_xfer"]


def test_meas_positional_mapping_and_types(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        m = lsc.meas()                                    # auto-describes first
    assert m["p_loadlock"] == pytest.approx(8.4e-07)
    assert isinstance(m["p_loadlock"], float)
    assert m["gv_fib"] == 1 and isinstance(m["gv_fib"], int)   # bool → 0/1
    assert m["fib_state"] == "idle"                       # enum → option string


def test_meas_nan_is_honoured(fake):
    fake.meas["p_loadlock"] = float("nan")                # invalid / unavailable
    with LSC("/dev/ttyUSB0") as lsc:
        m = lsc.meas()
    assert math.isnan(m["p_loadlock"])


def test_meas_arity_mismatch_raises(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        lsc.describe()
        fake.order = ["p_loadlock", "gv_fib"]             # instrument drops fields
        with pytest.raises(LSCError, match="arity"):
            lsc.meas()


# -- controller: control verbs ----------------------------------------------- #

def test_command_ok_and_err_parsing(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        assert lsc.command("vent", "ON") == "OK"
        assert fake.sink_state["vent"] == 1
        with pytest.raises(LSCError) as ei:
            lsc.command("bogus", "DO")                    # =ERR,2,"Unknown sink"
        assert ei.value.code == 2


def test_command_setpoint_passes_arg(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        assert lsc.command("pump", "SET", 3) == "OK"
    assert "pump:SET 3" in fake.commands                  # the arg actually went on the wire


def test_stream_on_off(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        assert lsc.stream(100) == "OK"
        assert fake.streaming is True
        assert lsc.stream_off() == "OK"
        assert fake.streaming is False


# -- controller: demux (=/#/!) ----------------------------------------------- #

def test_read_message_classifies_by_prefix(fake):
    with LSC("/dev/ttyUSB0") as lsc:
        lsc.describe()
        fake.inject("#142530,8.4e-07,1,1,idle")
        frame = lsc.read_message()
        assert isinstance(frame, StreamFrame)
        assert frame.ms == 142530
        assert frame.values["p_loadlock"] == pytest.approx(8.4e-07)
        assert frame.values["gv_fib"] == 1
        assert frame.values["fib_state"] == "idle"

        fake.inject("!EVT,100,leak,chamber A")
        evt = lsc.read_message()
        assert isinstance(evt, Event)
        assert (evt.ms, evt.kind, evt.detail) == (100, "leak", "chamber A")


def test_event_before_reply_is_surfaced_and_reply_still_returned(fake):
    """A pending '=' reply is taken as the next '=' line while interleaved '!'
    events are surfaced via the callback — the demux contract (manual §2)."""
    events = []
    with LSC("/dev/ttyUSB0", on_event=events.append) as lsc:
        lsc.describe()
        fake.inject("!EVT,142530,gv-open,FIB Gate Valve")   # arrives before reply
        m = lsc.meas()
        assert m["p_loadlock"] == pytest.approx(8.4e-07)     # reply still parsed
    assert len(events) == 1
    assert events[0].kind == "gv-open" and events[0].ms == 142530
    assert events[0].detail == "FIB Gate Valve"


# -- discovery ---------------------------------------------------------------- #

def test_probe_port_identifies_reads_schema_and_closes(fake):
    res = probe_port("/dev/ttyUSB0")
    assert res is not None
    assert res.serial == "0001" and res.firmware == "demo-0.1"
    assert res.product == "LSCIAKTHFIHELIOS"
    assert [s["id"] for s in res.schema["sources"]][0] == "p_loadlock"
    assert not fake.is_open                                # probe never holds the port


# -- BaseDevice wrapper: self-describing construction ------------------------ #

def _device(fake) -> LSCDevice:
    dev = LSCDevice(ProbeResult(port="/dev/ttyUSB0", serial="0001",
                                firmware="demo-0.1", product="LSCIAKTHFIHELIOS",
                                schema=json.loads(json.dumps(SCHEMA))))
    dev._connect()                                       # starts the background reader thread
    fake.devices.append(dev)                             # → disconnected on fixture teardown
    return dev


def _read(dev, sid):
    src = next(s for s in dev._sources if s.id == sid)
    return dev._read(src)


def test_device_builds_sources_dynamically_from_schema(fake):
    dev = _device(fake)
    by_id = {s.id: s for s in dev._sources}
    assert set(by_id) == {"p_loadlock", "gv_fib", "pump", "fib_state"}
    assert by_id["p_loadlock"].modality == Modality.SCALAR      # f32 → scalar
    assert by_id["p_loadlock"].prefer_log is True              # from "log": true
    assert by_id["gv_fib"].modality == Modality.STATUS         # bool → status
    assert by_id["fib_state"].modality == Modality.STATUS      # enum → status


def test_device_builds_sinks_dynamically_from_schema(fake):
    dev = _device(fake)
    by_id = {s.id: s for s in dev._sinks}
    assert by_id["vent"].kind == SinkKind.TOGGLE
    assert by_id["fib_xfer"].kind == SinkKind.ACTION


def test_sink_mapping_covers_setpoint_and_enum():
    sp = _sink_from_schema({"id": "bias", "name": "Bias", "kind": "setpoint",
                            "min": 0.0, "max": 5.0, "unit": "V"})
    assert sp.kind == SinkKind.SETPOINT
    assert sp.params[0].minimum == 0.0 and sp.params[0].maximum == 5.0
    en = _sink_from_schema({"id": "mode", "name": "Mode", "kind": "enum",
                            "options": ["a", "b", "c"]})
    assert en.kind == SinkKind.ENUM
    assert en.params[0].options == ("a", "b", "c")


# -- BaseDevice wrapper: data plane ------------------------------------------ #

def test_device_reads_all_sources_from_one_meas(fake):
    dev = _device(fake)
    fake.meas_count = 0
    values = {sid: _read(dev, sid)
              for sid in ("p_loadlock", "gv_fib", "pump", "fib_state")}
    assert fake.meas_count == 1                            # ONE MEAS? served the cycle
    assert values["p_loadlock"] == (pytest.approx(8.4e-07), 0)
    assert values["gv_fib"] == (pytest.approx(1.0), 0)     # bool → 1.0
    assert values["fib_state"] == (pytest.approx(0.0), 0)  # enum "idle" → index 0


def test_device_enum_unknown_option_is_bad(fake):
    fake.meas["fib_state"] = "nan"                         # unavailable enum
    dev = _device(fake)
    v, st = _read(dev, "fib_state")
    assert math.isnan(v) and st == 1


def test_device_seeds_sink_values_from_real_state(fake):
    fake.sink_state = {"vent": 0, "rough": 0, "gv_fib": 1, "pump": 1}
    dev = _device(fake)
    assert dev._sink_values["pump"] is True                # seeded from :STATE?
    assert dev._sink_values["vent"] is False


# -- BaseDevice wrapper: control --------------------------------------------- #

def test_device_write_toggle_sends_command(fake):
    dev = _device(fake)
    sink = next(s for s in dev._sinks if s.id == "vent")
    dev._write(sink, True)
    assert fake.sink_state["vent"] == 1
    dev._write(sink, False)
    assert fake.sink_state["vent"] == 0


def test_device_refused_write_raises_never_silent(fake):
    dev = _device(fake)
    fake.refuse = True
    sink = next(s for s in dev._sinks if s.id == "vent")
    with pytest.raises(LSCError, match="error 4"):
        dev._write(sink, True)


# -- BaseDevice wrapper: events → device-origin tags ------------------------- #

def test_event_becomes_device_origin_tag(fake):
    dev = _device(fake)
    tags = []
    dev.set_tag_sink(tags.append)
    fake.inject("!EVT,142760,fib-request,FIB")            # reader thread delivers it
    assert _spin(lambda: len(tags) == 1)
    tag = tags[0]
    assert tag.kind == "fib-request"
    assert tag.origin_kind == ORIGIN_DEVICE
    assert tag.origin_id == dev.data_id
    assert tag.payload["ms_since_boot"] == 142760


def test_leak_event_maps_to_critical_severity(fake):
    dev = _device(fake)
    tags = []
    dev.set_tag_sink(tags.append)
    fake.inject("!EVT,999,leak,chamber")
    assert _spin(lambda: len(tags) == 1)
    assert tags[0].severity == "critical"


@pytest.mark.parametrize("kind, detail, expected", [
    ("vent-close", "Vent Valve", "Vent Valve closed"),      # issue #8: name the transition,
    ("pump-on", "Scroll Pump", "Scroll Pump on"),           # not just the actuator
    ("gv-open", "FIB Gate Valve", "FIB Gate Valve opened"),
])
def test_event_tag_label_names_the_transition(fake, kind, detail, expected):
    """The tag label composes actuator + action so the timeline reads 'Vent Valve
    closed', not 'Vent Valve' (issue #8). The action suffix is parsed off the kind
    slug generically — no per-actuator table."""
    dev = _device(fake)
    tags = []
    dev.set_tag_sink(tags.append)
    fake.inject(f"!EVT,1000,{kind},{detail}")
    assert _spin(lambda: len(tags) == 1)
    assert tags[0].label == expected
    assert tags[0].kind == kind                             # kind still the raw slug


def test_event_tag_label_falls_back_when_detail_empty(fake):
    """An unknown action suffix or a missing actuator name degrades to the raw kind,
    so the label is never worse than the old `detail or kind` behaviour (issue #8)."""
    dev = _device(fake)
    tags = []
    dev.set_tag_sink(tags.append)
    fake.inject("!EVT,1000,fib-timeout,")                   # no detail → old behaviour: the kind
    assert _spin(lambda: len(tags) == 1)
    assert tags[0].label == "fib-timeout"                   # not a leading-space " fib-timeout"


def test_event_delivered_without_any_poll(fake):
    """The fix for #4: an idle, never-polled device still delivers a front-panel event as
    a tag in real time — the reader thread surfaces it independent of MEAS?/_read."""
    dev = _device(fake)
    tags = []
    dev.set_tag_sink(tags.append)
    assert fake.meas_count == 0                           # nothing has polled the device
    fake.inject("!EVT,5000,vent-close,Vent Valve")        # a physical button press
    assert _spin(lambda: len(tags) == 1)                  # …becomes a tag with no poll at all
    assert fake.meas_count == 0
    assert tags[0].kind == "vent-close" and tags[0].origin_kind == ORIGIN_DEVICE


# -- fixes: platform dtype, robustness, resync, ERR-match -------------------- #

def test_source_dtype_is_platform_float_for_enum_and_i32():
    """enum/i32 sources carry a FLOAT on the data plane, so their ferroDAC dtype must be
    a recognized one ("float"), NOT "str"/"int" — otherwise the router drops the channel
    from curation/export/charts. bool stays "bool" (a recognized dtype)."""
    en = mod._source_from_schema({"id": "st", "name": "State", "dtype": "enum",
                                  "options": ["a", "b"]})
    assert en.dtype == "float" and en.modality == Modality.STATUS
    i32 = mod._source_from_schema({"id": "n", "name": "Count", "dtype": "i32"})
    assert i32.dtype == "float" and i32.modality == Modality.SCALAR
    bl = mod._source_from_schema({"id": "b", "name": "On", "dtype": "bool"})
    assert bl.dtype == "bool"


def test_malformed_meas_value_is_nan_not_crash(fake):
    """A garbage (non-numeric, non-'nan') MEAS field maps to NaN, never a bare
    ValueError out of meas() — _convert is total (mirrors the invalid→nan contract)."""
    fake.meas["p_loadlock"] = "OL"                        # sensor overrange token, not a number
    with LSC("/dev/ttyUSB0") as lsc:
        m = lsc.meas()
    assert math.isnan(m["p_loadlock"])


def test_malformed_stream_frame_does_not_raise_out_of_command(fake):
    """A malformed '#' frame interleaved before a reply must NOT raise a bare ValueError
    out of the in-flight command — its bad value maps to NaN and the reply still returns."""
    frames = []
    with LSC("/dev/ttyUSB0", on_stream=frames.append) as lsc:
        lsc.describe()
        fake.inject("#5000,OL,1,1,idle")                  # arity-ok but first field is garbage
        m = lsc.meas()
        assert m["p_loadlock"] == pytest.approx(8.4e-07)  # reply still parsed
    assert len(frames) == 1 and math.isnan(frames[0].values["p_loadlock"])


def test_device_enum_value_not_in_options_is_bad(fake):
    """An enum reading whose string is NOT one of the options → (nan,1) via the
    opts.index branch (distinct from the 'nan' sentinel branch)."""
    fake.meas["fib_state"] = "surprise"                   # a real string, not in options, not nan
    dev = _device(fake)
    v, st = _read(dev, "fib_state")
    assert math.isnan(v) and st == 1


def test_reply_beginning_ERR_but_not_an_error_is_data(fake, monkeypatch):
    """A value that merely begins with 'ERR' (e.g. an enum state 'ERR_LEAK') is DATA,
    not an instrument error — only 'ERR,'/'ERR' is an error (aligns with lsa31)."""
    with LSC("/dev/ttyUSB0") as lsc:
        monkeypatch.setattr(lsc, "_await_reply", lambda: "ERR_LEAK")
        assert lsc.command("fib_state", "STATE?") == "ERR_LEAK"     # returned as data
        monkeypatch.setattr(lsc, "_await_reply", lambda: 'ERR,4,"refused"')
        with pytest.raises(LSCError) as ei:
            lsc.command("vent", "ON")
        assert ei.value.code == 4                          # a real =ERR still raises


def test_timeout_desyncs_and_next_send_resyncs(fake):
    """A reply timeout flags the link desynced; the next send discards any late/stale
    reply first, so a command can never read the PREVIOUS command's reply."""
    with LSC("/dev/ttyUSB0") as lsc:
        fake._buf = bytearray()                           # nothing to read → timeout
        with pytest.raises(LSCError, match="timeout"):
            lsc._await_reply()
        assert lsc._desynced is True
        fake._buf = bytearray(b"=STALE\n")                # a late reply from the timed-out cmd
        lsc._send("MEAS?")                                # resync: flush stale, then send
        assert lsc._desynced is False
        assert b"STALE" not in fake._buf                  # stale reply discarded…
        assert fake._buf.startswith(b"=")                 # …only the fresh MEAS? reply remains


def test_query_err_surface_raises_with_code(fake):
    """describe/meas/state/stream errors surface as LSCError with the parsed code."""
    with LSC("/dev/ttyUSB0") as lsc:
        lsc.describe()
        with pytest.raises(LSCError) as ei:
            lsc.state("nonexistent")                       # =ERR,2,"No such state"
        assert ei.value.code == 2


def test_first_connect_retries_past_a_transient_idn_err(fake):
    """A first *IDN? that returns ERR (a first-write-after-open transient / a device-side
    partial-line leftover) no longer fails the connect — the shared handshake retries past
    it, so no remove/re-add is needed (issue #9)."""
    fake.idn_fail = 1
    assert probe_port("/dev/ttyUSB0") is not None        # discovery recovers past the ERR
    fake.idn_fail = 1
    dev = _device(fake)                                  # …and so does _connect
    assert dev._lsc is not None and dev._firmware == "demo-0.1"


def test_warmup_writes_lone_terminator_before_first_idn(fake):
    """The handshake sends a lone terminator (flushing the instrument parser) before the
    first *IDN? (issue #9)."""
    probe_port("/dev/ttyUSB0")
    assert "" in fake.commands                           # a blank line went out…
    assert fake.commands.index("") < fake.commands.index("*IDN?")   # …before the first *IDN?


def test_first_connect_all_retries_fail_behaves_as_today(fake):
    """When every *IDN? fails, discovery returns None and _connect raises + leaves the port
    clean — exactly as before the retry helper (issue #9, never-worse)."""
    from ferrodac.core.serial_arbiter import PORTS_IN_USE
    fake.idn_fail = 99
    assert probe_port("/dev/ttyUSB0") is None
    fake.idn_fail = 99
    dev = LSCDevice(ProbeResult(port="/dev/ttyUSB0", serial="0001", firmware="d",
                                product="P", schema=json.loads(json.dumps(SCHEMA))))
    with pytest.raises(RuntimeError, match="not an LSC"):
        dev._connect()
    assert "/dev/ttyUSB0" not in PORTS_IN_USE            # arbiter untouched on failure


def test_reader_hard_resyncs_after_timeout(fake):
    """SAFETY (concurrency review): after a reader-mode command times out, its LATE reply
    must not be read by the NEXT command — otherwise a refused write could look accepted.
    The hard resync (stop→flush buffer+queue→restart reader) drops the stale reply."""
    lsc = LSC("/dev/ttyUSB0", timeout=0.1)               # short timeout → fast test
    lsc.open()
    lsc.describe()                                        # synchronous handshake (reader off)
    lsc.start_reader()
    try:
        fake.drop_next_reply = True                      # the device misses this command's reply
        with pytest.raises(LSCError, match="timeout"):
            lsc.command("vent", "ON")                    # → times out, flags a resync
        fake.inject("=OK")                               # the missed reply arrives LATE (stale)
        m = lsc.meas()                                   # the NEXT command hard-resyncs first…
        assert "p_loadlock" in m and "fib_state" in m    # …and reads ITS OWN MEAS reply, not "=OK"
    finally:
        lsc.close()


def test_disconnect_releases_port_even_if_close_raises(fake, monkeypatch):
    """A corrupted port that errors on close() must STILL be released from the shared
    arbiter — else the device stays 'in use' and becomes undiscoverable until restart (#6)."""
    from ferrodac.core.serial_arbiter import PORTS_IN_USE
    dev = LSCDevice(ProbeResult(port="/dev/ttyUSB9", serial="0009", firmware="d",
                                product="P", schema=json.loads(json.dumps(SCHEMA))))
    dev._connect()
    lsc = dev._lsc
    assert "/dev/ttyUSB9" in PORTS_IN_USE                 # held while active
    monkeypatch.setattr(lsc, "close",                     # simulate a corrupted FTDI on close
                        lambda: (_ for _ in ()).throw(OSError("bad port")))
    try:
        dev._disconnect()
        assert "/dev/ttyUSB9" not in PORTS_IN_USE         # …released anyway
    finally:
        lsc.stop_reader()                                 # close was stubbed → stop the reader
        PORTS_IN_USE.discard("/dev/ttyUSB9")


def test_discover_releases_vanished_port_from_arbiter(monkeypatch):
    """discover() drops a no-longer-present port from the shared arbiter (the #6 backstop)."""
    import ferrodac.devices.lsc as m
    from ferrodac.core.serial_arbiter import PORTS_IN_USE
    monkeypatch.setattr(m, "HAVE_SERIAL", True)
    monkeypatch.setattr(m.serial.tools.list_ports, "comports", lambda: [])   # nothing present
    monkeypatch.setattr(m, "probe_port", lambda p: None)
    m.LSCDevice._cache.pop("/dev/ghost", None)
    PORTS_IN_USE.add("/dev/ghost")                        # a port stuck in the arbiter
    try:
        m.LSCDevice.discover()
        assert "/dev/ghost" not in PORTS_IN_USE
    finally:
        PORTS_IN_USE.discard("/dev/ghost")


def test_discover_reprobes_a_failed_port_after_cooldown(monkeypatch):
    """A transiently-failed probe is cached but re-probed after PROBE_RETRY_S, so a live
    device can't be stuck undiscoverable forever (issue #6)."""
    import ferrodac.devices.lsc as m
    from ferrodac.core.serial_arbiter import PORTS_IN_USE

    class _P:
        def __init__(self, d):
            self.device = d
    monkeypatch.setattr(m, "HAVE_SERIAL", True)
    monkeypatch.setattr(m.serial.tools.list_ports, "comports", lambda: [_P("/dev/probe0")])
    n = {"calls": 0}

    def _probe(_p):
        n["calls"] += 1
        return None                                       # always fails
    monkeypatch.setattr(m, "probe_port", _probe)
    m.LSCDevice._cache.pop("/dev/probe0", None)
    m.LSCDevice._probe_cooldown.pop("/dev/probe0", None)
    PORTS_IN_USE.discard("/dev/probe0")
    try:
        m.LSCDevice.discover()                            # probe #1 → fail → cached + cooldown
        assert n["calls"] == 1
        m.LSCDevice.discover()                            # within cooldown → NOT re-probed
        assert n["calls"] == 1
        m.LSCDevice._probe_cooldown["/dev/probe0"] = 0.0  # fast-forward past the cooldown
        m.LSCDevice.discover()                            # cooldown expired → re-probed
        assert n["calls"] == 2
    finally:
        m.LSCDevice._cache.pop("/dev/probe0", None)
        m.LSCDevice._probe_cooldown.pop("/dev/probe0", None)


def test_open_is_exclusive_on_posix_only(monkeypatch):
    """A live link opens with POSIX exclusive access so a stray reader can't corrupt it (#6);
    non-POSIX (Windows serial is exclusive by default) passes no such kwarg."""
    import ferrodac.devices.lsc as m
    captured = {}
    monkeypatch.setattr(m.serial, "Serial",
                        lambda port, baud, **kw: (captured.update(kw), FakeSerial())[1])
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m.sys, "platform", "linux")
    LSC("/dev/ttyUSB0").open()
    assert captured.get("exclusive") is True
    captured.clear()
    monkeypatch.setattr(m.sys, "platform", "win32")
    LSC("/dev/ttyUSB0").open()
    assert "exclusive" not in captured


def test_device_write_action_sends_do(fake):
    """A device-level ACTION write sends '<sink>:DO' (only TOGGLE was covered)."""
    dev = _device(fake)
    action = next(s for s in dev._sinks if s.id == "fib_xfer")
    fake.commands.clear()
    dev._write(action, None)
    assert "fib_xfer:DO" in fake.commands
