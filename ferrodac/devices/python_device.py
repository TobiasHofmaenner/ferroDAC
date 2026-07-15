"""Python Device — a virtual device whose "config" is a block of Python that the
app EXECUTES on the device poll thread; each value it returns becomes a Reading and
routes like any other source.

This is the *bring-your-own-signal* driver: paste some Python (a math function, an
HTTP/webhook fetch, a computed KPI) and it shows up as one or more live channels. It
is added from the Devices menu (not via discovery — the discovery worker purges the
available set), persisted to a small JSON defs file so it survives a restart, and
configured entirely through its single ``code`` option.

Contract the pasted code follows (all optional except ``poll``):

  * ``SOURCES`` / ``CHANNELS`` — a module-level list declaring the channels, each a
    dict ``{"id", "name", "unit", "prefer_log"}`` (or a bare string id). If absent, a
    ``sources()`` function may return the same; if that's absent too, the channels are
    inferred from a single ``poll`` (a dict -> its keys, a scalar -> one ``value``).
  * ``setup(ctx)`` — runs once each time the code is (re)compiled.
  * ``poll(ctx)`` — runs once per sample cycle; returns ``{source_id: value}`` (or a
    single number for a one-channel source). Called ONCE per cycle and cached, so every
    channel shares one poll.

``ctx`` exposes: ``ctx.t`` (wall time, s), ``ctx.state`` (a dict persisted across
polls — stash counters / clients / an HTTP session here), ``ctx.last`` (the previous
poll's dict result), ``ctx.log(msg)`` (a message shown in Check), and
``ctx.option(key)`` (read a device option by key).

TRUST MODEL — there is **no sandbox**. The code runs in-process on the acquisition
thread with the full authority of the app, exactly like a processor or an extension.
Only run code you would run yourself. v1 is deliberately honest about this rather than
pretending to a security boundary it does not have.
"""

from __future__ import annotations

import builtins
import collections
import json
import logging
import os
import time
import traceback
import uuid

from ..core.base import BaseDevice
from ..core.connectors import default_config_dir
from ..core.device import (
    CheckResult,
    Interface,
    Option,
    RateControl,
    RateMode,
    Sink,
    SinkKind,
    Source,
)

log = logging.getLogger("ferrodac.python_device")

# SINKS declaration kind string -> SinkKind (the control-sink types the app renders).
_SINK_KINDS = {"action": SinkKind.ACTION, "setpoint": SinkKind.SETPOINT,
               "toggle": SinkKind.TOGGLE, "enum": SinkKind.ENUM}


# --------------------------------------------------------------------------- #
#  Starter template (the default `code` option)
# --------------------------------------------------------------------------- #
STARTER_CODE = '''\
"""Python Device — this code runs on the device poll thread. Each value returned
by poll(ctx) becomes a Reading and routes like any other source.

  SOURCES     (optional)  a list of {"id","name","unit"} dicts declaring channels.
  setup(ctx)  (optional)  runs once when the code is (re)compiled.
  poll(ctx)   (required)  runs once per sample; return {source_id: value}
                          (or a single number for a one-channel source).
  SINKS       (optional)  control inputs to advertise: [{"id","name","kind"}],
                          kind = setpoint | toggle | enum | action.
  write(ctx, sink_id, value)  (optional)  runs when a sink is set (device.set_sink
                          or the UI) — do something with value (POST, drive hardware).

ctx.t         wall-clock time (seconds)
ctx.state     a dict that persists across polls (counters / clients live here)
ctx.last      the previous poll's dict result
ctx.log(m)    log a message (shown by the Check button)
ctx.option(k) read a device option by key
ctx.sink(k)   read a control sink's current value (what was last written to it)

NOTE: this code runs IN-PROCESS with full trust, the same as a plugin. There is no
sandbox -- only paste code you would run yourself.
"""

import math

SOURCES = [
    {"id": "sine", "name": "Sine", "unit": ""},
    {"id": "ramp", "name": "Ramp", "unit": ""},
]


def setup(ctx):
    ctx.state["t0"] = ctx.t
    ctx.state["n"] = 0


def poll(ctx):
    t = ctx.t - ctx.state.get("t0", ctx.t)
    ctx.state["n"] = ctx.state.get("n", 0) + 1
    return {
        "sine": math.sin(t),
        "ramp": ctx.state["n"] % 100,
    }


# --- Example: read a number from an HTTP / webhook endpoint -----------------
# import json, urllib.request
#
# SOURCES = [{"id": "temp", "name": "Outside temp", "unit": "C"}]
#
# def poll(ctx):
#     url = ctx.option("url") or "https://api.example.com/latest"
#     with urllib.request.urlopen(url, timeout=5) as r:
#         data = json.load(r)
#     return {"temp": data["temperature"]}
#
# --- Example: a device with a controllable setpoint (a SINK) ----------------
# SINKS   = [{"id": "target", "name": "Target", "kind": "setpoint", "value": 0.0}]
# SOURCES = [{"id": "target_rb", "name": "Target readback", "unit": ""}]
#
# def write(ctx, sink_id, value):
#     ctx.log(f"set {sink_id} = {value}")        # e.g. POST it to an instrument
#
# def poll(ctx):
#     return {"target_rb": ctx.sink("target")}   # read the setpoint back
'''


# --------------------------------------------------------------------------- #
#  Persistence — a small JSON defs file: {instance_id: code}
# --------------------------------------------------------------------------- #
def defs_path() -> str:
    """Path of the defs file that lets Python devices survive a restart."""
    return os.path.join(default_config_dir(), "python_devices.json")


def load_defs() -> dict:
    """{instance_id: code} from disk (empty on a missing / corrupt file)."""
    try:
        with open(defs_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def _write_defs(defs: dict) -> None:
    path = defs_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(defs, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)               # owner-only (code can carry secrets)
    except OSError:
        pass
    os.replace(tmp, path)                  # atomic


def save_def(instance_id: str, code: str) -> None:
    """Persist one source's code (atomic, 0600). Best-effort: a write failure logs
    and is swallowed so a full disk never kills a live device."""
    try:
        defs = load_defs()
        defs[str(instance_id)] = str(code)
        _write_defs(defs)
    except OSError as exc:                  # noqa: BLE001
        log.warning("python_device: could not persist %s: %s", instance_id, exc)


def delete_def(instance_id: str) -> None:
    """Forget one source (called when the device is removed). Best-effort."""
    try:
        defs = load_defs()
        if str(instance_id) in defs:
            del defs[str(instance_id)]
            _write_defs(defs)
    except OSError as exc:                  # noqa: BLE001
        log.warning("python_device: could not delete %s: %s", instance_id, exc)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _last_line(tb: str) -> str:
    """The last non-blank line of a traceback — the human-facing error summary."""
    lines = [ln.strip() for ln in (tb or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "error"


def _slug(text: str) -> str:
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(text)).strip("_")
    return out.lower() or "value"


class _Ctx:
    """The small object handed to setup(ctx) / poll(ctx). Deliberately tiny and
    JSON-agnostic — user code stashes whatever it likes in ``state``."""

    def __init__(self, device: "PythonDevice"):
        self._device = device
        self.state: dict = {}
        self._last: dict = {}
        self._logs = collections.deque(maxlen=50)

    @property
    def t(self) -> float:
        """Wall-clock time in seconds (evaluated on access)."""
        return time.time()

    @property
    def last(self) -> dict:
        """The previous poll's result as a dict (a copy — safe to mutate)."""
        return dict(self._last)

    def log(self, msg) -> None:
        self._logs.append(str(msg))
        log.debug("python_device[%s]: %s", self._device.instance_id, msg)

    def option(self, key: str):
        """Read a device option by key (e.g. a URL you added as an option)."""
        return self._device._option_values.get(key)

    def sink(self, key: str):
        """The current value of a control SINK — what someone last wrote to it (via
        device.set_sink or the UI). Lets poll() read a setpoint/toggle back."""
        return self._device._sink_values.get(key)


# --------------------------------------------------------------------------- #
#  The driver
# --------------------------------------------------------------------------- #
class PythonDevice(BaseDevice):
    """A virtual device whose channels are produced by user-supplied Python.

    discoverable=False (it is minted from the Devices menu, never scanned) and
    async_config=True (compiling + a probe poll may block, so the manager applies the
    ``code`` option off the GUI thread)."""

    driver = "python_device"
    discoverable = False
    async_config = True                    # (re)compiling + a probe poll can block

    def __init__(self, instance_id: str, code: str, name: str = "Python Device"):
        # State the exec model needs, set BEFORE super().__init__ (which builds the
        # placeholder sources) so a first _recompile below can replace them safely.
        self._ns: dict = {}                # the compiled user namespace
        self._ctx = _Ctx(self)             # replaced by _build() on a good compile
        self._cache_val = None             # this cycle's poll result (dict|scalar|None)
        self._cache_t = -1e9               # monotonic time it was produced
        self._cache_valid = False
        self._runtime_err = False          # poll error<->ok edge tracker (for last_error)

        super().__init__(
            instance_id=instance_id,
            name=name,
            interface=Interface(kind="software", params={}),
            sources=[Source(id="value", name="value")],   # replaced by _recompile
            options=[Option("code", "Python", value=code, kind="text")],
            rate=RateControl(mode=RateMode.SETTABLE, default_hz=1.0,
                             min_hz=0.1, max_hz=20.0),
            hardware_id=instance_id,        # STABLE fingerprint == the id (restore/dedup)
            model="Python device",
            manufacturer="ferroDAC",
        )
        self._recompile(code)

    # -- minting / restore ---------------------------------------------------
    @classmethod
    def new(cls, code: "str | None" = None, name: str = "Python Device") -> "PythonDevice":
        """A fresh instance with a unique id. Defaults to the starter template; pass
        ``code`` to create it pre-populated (e.g. the device.create verb over the API).
        The caller then persists it with ``save_def(dev.instance_id, dev.code)``."""
        return cls(instance_id=f"python:{uuid.uuid4().hex[:8]}",
                   code=code if code else STARTER_CODE, name=str(name or "Python Device"))

    @classmethod
    def restore(cls, instance_id: str, code: "str | None" = None) -> "PythonDevice":
        """Rebuild a saved source. ``code`` defaults to the persisted def (then the
        starter, if the id is unknown)."""
        if code is None:
            code = load_defs().get(instance_id, STARTER_CODE)
        return cls(instance_id=instance_id, code=code)

    @classmethod
    def restore_all(cls) -> "list[PythonDevice]":
        """Every persisted Python device — call on startup to rehydrate the active set."""
        return [cls(instance_id=iid, code=code) for iid, code in load_defs().items()]

    def on_forget(self) -> None:
        """Manager cleanup hook, called on a user remove: drop the persisted def so
        this source doesn't come back on the next launch. (Driver-agnostic name so the
        manager needn't import this module.)"""
        delete_def(self._instance_id)

    # -- config (code) -------------------------------------------------------
    @property
    def code(self) -> str:
        """The current user code (the ``code`` option's value)."""
        return self._option_values.get("code") or ""

    def _on_option(self, key: str, value) -> None:
        if key == "code":
            self._recompile(value)
            if self._last_error is None:          # persist only last-GOOD code, so a
                save_def(self._instance_id, value)  # broken edit can't lose channels on restart

    def _build(self, text: str):
        """Compile + exec ``text`` into a fresh namespace, run setup(ctx), derive the
        sources. Returns ``(ns, ctx, sources)``; RAISES on any stage (syntax, exec,
        setup, or an inference poll). The caller decides what a failure means."""
        ns = {"__name__": "ferrodac_python_device", "__builtins__": builtins}
        exec(compile(text, "<python_device>", "exec"), ns)   # noqa: S102 — by design
        ctx = _Ctx(self)
        setup = ns.get("setup")
        if callable(setup):
            setup(ctx)
        sources = self._derive_sources(ns, ctx)
        sinks = self._derive_sinks(ns)
        return ns, ctx, sources, sinks

    def _derive_sources(self, ns: dict, ctx: _Ctx) -> list:
        """Channels from (in order): a SOURCES/CHANNELS list, a sources() function,
        else inference from one poll (dict -> keys, scalar -> a single ``value``)."""
        spec = ns.get("SOURCES", ns.get("CHANNELS"))
        if spec is None and callable(ns.get("sources")):
            fn = ns["sources"]
            try:
                spec = fn(ctx)
            except TypeError:
                spec = fn()
        if spec is not None:
            return [self._make_source(item) for item in spec]
        poll = ns.get("poll")
        if callable(poll):
            result = poll(ctx)                     # inference probe (may raise -> caller)
            if isinstance(result, dict):
                return [self._make_source(k) for k in result]
        return [self._make_source("value")]

    @staticmethod
    def _make_source(item) -> Source:
        if isinstance(item, dict):
            sid = str(item.get("id") or _slug(item.get("name", "")))
            name = str(item.get("name") or sid)
            return Source(id=sid or "value", name=name,
                          unit=str(item.get("unit", "") or ""),
                          prefer_log=bool(item.get("prefer_log", False)))
        sid = str(item)
        return Source(id=sid, name=sid)

    def _derive_sinks(self, ns: dict) -> list:
        """Control sinks from an optional module-level SINKS list. Each item is a dict
        {id, name?, kind?, value?, choices?}; kind ∈ action/setpoint/toggle/enum
        (default setpoint; a bare string id = a setpoint). Absent SINKS -> no sinks."""
        spec = ns.get("SINKS")
        return [self._make_sink(item) for item in spec] if spec else []

    @staticmethod
    def _make_sink(item) -> Sink:
        if not isinstance(item, dict):
            item = {"id": str(item)}
        sid = str(item.get("id") or _slug(item.get("name", ""))) or "sink"
        kind = _SINK_KINDS.get(str(item.get("kind", "setpoint")).lower(), SinkKind.SETPOINT)
        params = tuple(item.get("choices") or item.get("params") or ())   # ENUM options
        return Sink(id=sid, name=str(item.get("name") or sid),
                    kind=kind, params=params, value=item.get("value"))

    def _recompile(self, text: str) -> None:
        """Apply new code. On success: swap in the namespace, ctx and sources, clear
        the error, invalidate the cache. On ANY failure: set ``last_error`` to the
        last traceback line and KEEP the previous namespace + sources (a broken edit
        never blanks a live device). Either way, re-announce (_mark_sink_dirty)."""
        try:
            ns, ctx, sources, sinks = self._build(text or "")
        except Exception:                          # noqa: BLE001 — surface, don't crash
            self._last_error = _last_line(traceback.format_exc())
            log.warning("python_device[%s]: compile failed: %s",
                        self._instance_id, self._last_error)
            self._mark_sink_dirty()                # re-announce so the UI shows the error
            return
        if not sources:                            # never leave a zombie with 0 channels
            sources = [Source(id="value", name="value")]
        self._ns, self._ctx, self._sources, self._sinks = ns, ctx, sources, sinks
        prev = getattr(self, "_sink_values", {})   # keep values for ids that persist
        self._sink_values = {s.id: prev.get(s.id, s.value)
                             for s in sinks if s.kind != SinkKind.ACTION}
        self._cache_valid = False
        self._runtime_err = False
        self._last_error = None
        self._mark_sink_dirty()

    # -- data plane ----------------------------------------------------------
    def _poll_cycle(self):
        """Run poll(ctx) at most ONCE per cycle and cache the result. Every channel's
        _read shares it (like the shelly bulk-status cache). Never raises — poll errors
        become a None result + a surfaced last_error on the error<->ok edge."""
        now = time.monotonic()
        interval = 1.0 / (self._rate_hz or 1.0)
        if self._cache_valid and (now - self._cache_t) < interval * 0.5:
            return self._cache_val
        self._cache_t = now
        self._cache_valid = True
        poll = self._ns.get("poll")
        if not callable(poll):
            self._cache_val = None
            self._fail("no poll(ctx) is defined")
            return None
        try:
            result = poll(self._ctx)
        except Exception:                          # noqa: BLE001 — a raising poll -> NaN
            self._cache_val = None
            self._fail(_last_line(traceback.format_exc()))
            return None
        if isinstance(result, dict):
            self._ctx._last = dict(result)
        elif result is not None:
            self._ctx._last = {"value": result}
        self._cache_val = result
        self._ok()
        return result

    def _fail(self, msg: str) -> None:
        """Poll transitioned OK -> error: surface it once (re-announce the descriptor)."""
        if not self._runtime_err:
            self._runtime_err = True
            self._last_error = msg
            self._mark_sink_dirty()

    def _ok(self) -> None:
        """Poll transitioned error -> OK: clear the error once."""
        if self._runtime_err:
            self._runtime_err = False
            self._last_error = None
            self._mark_sink_dirty()

    def _read(self, source: Source):
        result = self._poll_cycle()
        if result is None:
            return float("nan"), 1
        val = result.get(source.id) if isinstance(result, dict) else result
        if val is None:
            return float("nan"), 1
        try:
            return float(val), 0
        except (TypeError, ValueError):
            return float("nan"), 1

    def _write(self, schema, value) -> None:
        """A control sink was written (device.set_sink / the UI). Dispatch to the user's
        write(ctx, sink_id, value) if defined. BaseDevice.write() stores the value in
        _sink_values AFTER this returns (so poll() reads it back via ctx.sink(id)); a
        device with no write() still 'holds' the setpoint that way. A raising write()
        propagates as a write failure to the caller."""
        fn = self._ns.get("write")
        if callable(fn):
            fn(self._ctx, schema.id, value)

    # -- diagnostics ---------------------------------------------------------
    def check(self) -> CheckResult:
        """Compile the current code and run poll once — the config GUI's "Check"
        button. Distinguishes a compile/setup error, a missing poll, a raising poll,
        and success (with the channel count + a note on what poll returned)."""
        code = self._option_values.get("code") or ""
        try:
            ns, ctx, sources, sinks = self._build(code)
        except Exception:                          # noqa: BLE001
            tb = traceback.format_exc()
            return CheckResult(False, f"Code error: {_last_line(tb)}", 0, detail=tb)
        poll = ns.get("poll")
        if not callable(poll):
            return CheckResult(False, "No poll(ctx) function is defined.", len(sources))
        try:
            result = poll(ctx)
        except Exception:                          # noqa: BLE001
            tb = traceback.format_exc()
            return CheckResult(False, f"poll(ctx) raised: {_last_line(tb)}",
                               len(sources), detail=tb)
        n, m = len(sources), len(sinks)
        if isinstance(result, dict):
            keys = ", ".join(str(k) for k in list(result)[:8])
            detail = f"poll() returned {len(result)} value(s): {keys}"
        else:
            detail = f"poll() returned a scalar ({result!r})."
        summary = (f"OK - compiled, {n} source{'' if n == 1 else 's'}"
                   + (f", {m} sink{'' if m == 1 else 's'}" if m else "")
                   + ", poll ran.")
        return CheckResult(True, summary, n, detail)
