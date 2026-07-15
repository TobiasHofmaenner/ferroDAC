"""Ferrovac LSC controller driver: the dependency-free LSC controller plus the
self-describing BaseDevice wrapper, exercised against a fake serial port that
emulates the instrument's line protocol (1,050,000/LF, demux by first byte) —
no hardware. Protocol reference: lsc-firmware docs/programming-manual.md (DRAFT)."""
import json
import math

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

    # -- pyserial surface the controller touches --
    def reset_input_buffer(self):
        self._buf = bytearray()

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def write(self, data: bytes):
        line = data.decode().strip()
        for resp in self._respond(line):
            self._buf += (resp + "\n").encode()
        return len(data)

    def read_until(self, term=b"\n"):
        idx = self._buf.find(term)
        if idx == -1:
            return b""                                   # timeout (no full line)
        end = idx + len(term)
        out = bytes(self._buf[:end])
        del self._buf[:end]
        return out

    def inject(self, text: str):
        """Push an unsolicited device→host line (event/stream frame)."""
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
        up = line.upper()
        if up == "*IDN?":
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
    """Route LSC.open at a FakeSerial; returns the shared instance."""
    fs = FakeSerial()
    monkeypatch.setattr(mod.serial, "Serial", lambda *a, **k: fs)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return fs


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
    dev._connect()
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
    fake.inject("!EVT,142760,fib-request,FIB")            # surfaced during MEAS?
    dev._meas = None
    _read(dev, "p_loadlock")
    assert len(tags) == 1
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
    dev._meas = None
    _read(dev, "p_loadlock")
    assert tags[0].severity == "critical"
