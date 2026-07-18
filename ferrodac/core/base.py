"""BaseDevice — convenience base implementing the common Device machinery.

Drivers usually subclass this: it holds the descriptor fields, a status
state-machine, current sink values, the configured sample rate, and the
acquisition loop. A driver implements `discover` + `_connect`/`_disconnect`
(+ `_read` for sources, `_write` for sinks).

Threading contract (DESIGN §21.1) — the platform owns it so drivers don't each
reinvent it (the audit found the TPG had to invent its own lock, which a
third-party author wouldn't know to do):

  * ``_connect``/``_disconnect``/``_read``/``_write`` are all serialized per
    device by ``self._io_lock`` (a re-entrant lock the platform provides and
    holds around each call). A driver never needs its own I/O lock — one device,
    one thread of hardware access at a time. Set ``serialize_io = False`` only if
    a driver deliberately manages its own concurrency.
  * These run on acquisition / config worker threads, **never the GUI thread** —
    so they must be Qt-free (never touch a QObject).
  * ``_throttle(key, interval)`` is the platform's reconnect/back-off helper: it
    returns True at most once per ``interval`` seconds for a given key, so a
    driver's link-reopen (or any retry) is rate-limited without a hand-rolled
    timestamp per driver.
  * Serial drivers additionally share ONE cross-driver port registry
    (core.serial_arbiter) so two drivers can't grab the same physical port.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Optional, Sequence

from .reading import Reading
from .identity import Fingerprint
from .device import (
    CheckResult,
    Device,
    DeviceDescriptor,
    Interface,
    Option,
    RateControl,
    RateMode,
    Sink,
    SinkKind,
    Source,
    Status,
)


class _NullGuard:
    """A no-op context manager for drivers that opt out of platform serialization."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL_GUARD = _NullGuard()


class BaseDevice(Device):
    driver = "base"   # registry skips this; real drivers override
    serialize_io = True   # platform serializes _read/_write/_connect per device
    reconnectable = False   # opt-in: the poll-loop supervisor auto-reconnects a dropped link
    #                         (DESIGN §21.1). ONLY for drivers whose _connect/_disconnect
    #                         reopen a physical link with no destructive side effects — never
    #                         sims / cloud / software / camera / drivers that self-heal.
    reconnect_backoff = 2.0   # seconds between throttled auto-reconnect attempts (§21.1)

    def __init__(
        self,
        instance_id: str,
        name: str,
        interface: Interface,
        sources: Sequence[Source] = (),
        sinks: Sequence[Sink] = (),
        rate: Optional[RateControl] = None,
        primary_source: Optional[str] = None,
        hardware_id: Optional[str] = None,
        model: Optional[str] = None,
        options: Sequence[Option] = (),
        manufacturer: Optional[str] = None,
        cal_date: Optional[str] = None,
        cal_due: Optional[str] = None,
        cal_cert: Optional[str] = None,
        asset_tag: Optional[str] = None,
    ):
        self._instance_id = instance_id
        self._name = name
        self._interface = interface
        self._sources = list(sources)
        # First-class uncertainty (DESIGN §19.0): the live σ MODEL per source, seeded
        # from each Source's declaration and re-declarable at runtime (set_uncertainty).
        self._uncertainty = {s.id: s.uncertainty for s in self._sources
                             if getattr(s, "uncertainty", None) is not None}
        self._provenance_dirty = False   # set when a model changes → app re-pushes
        self._sink_dirty = False         # set when a poll reads back a changed control state
        self._sinks = list(sinks)
        self._options = list(options)
        self._option_values = {o.key: o.value for o in self._options}
        self._rate = rate
        self._primary_source = primary_source
        self._hardware_id = hardware_id
        self._model = model
        # Lab-journal provenance a capable device self-reports (the rest stays None
        # and the user / device DB fills it in). See DeviceDescriptor.
        self._manufacturer = manufacturer
        self._cal_date = cal_date
        self._cal_due = cal_due
        self._cal_cert = cal_cert
        self._asset_tag = asset_tag
        self._firmware: Optional[str] = None
        self._status = Status.DISCOVERED
        self._last_error: Optional[str] = None
        self._uuid: Optional[str] = None       # data-plane identity (set at onboarding)

        self._sink_values = {
            s.id: s.value for s in self._sinks if s.kind != SinkKind.ACTION
        }
        self._rate_hz = rate.default_hz if rate else None

        self._streaming = False
        self._thread: Optional[threading.Thread] = None
        self._emit = None
        self._tag_sink = None            # platform-injected device→tag channel (§7.3)
        self._prompt_sink = None         # platform-injected device→app→device request channel
        self._prompt_withdraw_sink = None  # device resolved its own prompt → retire it from the inbox
        # Platform-owned per-device serialization (threading contract, above).
        # Re-entrant so a driver's own `with self._io_lock` (legacy) nests safely.
        self._io_lock = threading.RLock()
        self._throttle_at: dict = {}      # key -> next monotonic time it may fire

    # -- identity / description ----------------------------------------------
    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def data_id(self) -> str:
        """The identity Readings are keyed by: the UUID once onboarded, else the
        physical instance_id."""
        return self._uuid or self._instance_id

    @property
    def uuid(self) -> Optional[str]:
        return self._uuid

    def set_uuid(self, uuid: str) -> None:
        self._uuid = uuid

    @property
    def fingerprint(self) -> Fingerprint:
        return Fingerprint(self.driver, self._hardware_id or self._instance_id)

    @property
    def name(self) -> str:
        return self._name

    def set_name(self, name: str) -> None:
        self._name = name

    @property
    def status(self) -> Status:
        return self._status

    def describe(self) -> DeviceDescriptor:
        sinks = []
        for s in self._sinks:
            if s.kind == SinkKind.ACTION:
                sinks.append(s)
            else:
                sinks.append(replace(s, value=self._sink_values.get(s.id, s.value)))
        return DeviceDescriptor(
            instance_id=self._instance_id,
            uuid=self._uuid,
            driver=self.driver,
            name=self._name,
            interface=self._interface,
            status=self._status,
            hardware_id=self._hardware_id,
            model=self._model,
            firmware=self._firmware,
            manufacturer=self._manufacturer,
            cal_date=self._cal_date,
            cal_due=self._cal_due,
            cal_cert=self._cal_cert,
            asset_tag=self._asset_tag,
            sources=[replace(s, uncertainty=self._uncertainty.get(s.id, s.uncertainty))
                     for s in self._sources],
            sinks=sinks,
            options=[replace(o, value=self._option_values.get(o.key, o.value))
                     for o in self._options],
            rate=self._rate,
            rate_hz=self._rate_hz,
            primary_source=self._primary_source,
            last_error=self._last_error,
        )

    # -- lifecycle ------------------------------------------------------------
    def mark_connecting(self) -> None:
        self._status = Status.CONNECTING
        self._last_error = None

    def _guard(self):
        """Context manager serializing hardware access per device (or a no-op when
        a driver opts out with serialize_io=False). See the threading contract."""
        return self._io_lock if self.serialize_io else _NULL_GUARD

    def _throttle(self, key: str, interval: float) -> bool:
        """Rate-limit a retry/reconnect: returns True at most once per `interval`
        seconds for `key` (the platform's back-off helper — DESIGN §21.1). Thread-
        safe under the device's io lock."""
        with self._io_lock:
            now = time.monotonic()
            if now < self._throttle_at.get(key, 0.0):
                return False
            self._throttle_at[key] = now + interval
            return True

    def connect(self) -> None:
        self._status = Status.CONNECTING
        self._last_error = None
        try:
            with self._guard():
                self._connect()
            self._status = Status.CONNECTED
        except Exception as exc:
            self._status = Status.ERROR
            self._last_error = str(exc)
            raise

    def disconnect(self) -> None:
        self.stop()
        try:
            # Under the io guard: stop() joins the poll thread BOUNDEDLY, so a read
            # wedged past the join timeout may still be inside the port when we get
            # here — closing it mid-read is undefined on some platforms (Windows
            # pyserial close-during-read). The guard waits out the driver's own
            # read timeout instead (concurrency-audit finding).
            with self._guard():
                self._disconnect()
        finally:
            self._status = Status.DISCONNECTED

    # -- uncertainty (DESIGN §19.0) ------------------------------------------
    def set_uncertainty(self, source_id: str, model) -> None:
        """Re-declare a source's σ MODEL at runtime — e.g. a Keithley range change,
        where accuracy depends on the setting. Flags provenance dirty so the app
        re-pushes the record; the store's change-log then time-resolves which model
        was in effect at each point (device_record_at), no new time-series needed.
        Cheap + Qt-free, safe to call from a driver's worker thread."""
        if self._uncertainty.get(source_id) == model:
            return
        if model is None:
            self._uncertainty.pop(source_id, None)
        else:
            self._uncertainty[source_id] = model
        self._provenance_dirty = True

    def take_provenance_dirty(self) -> bool:
        """True (once) if a σ model changed since the last check — the app polls this
        after a write to decide whether to re-push the device record."""
        dirty, self._provenance_dirty = self._provenance_dirty, False
        return dirty

    def _mark_sink_dirty(self) -> None:
        """A driver calls this when a POLL reads back a control state that changed
        outside our writes (e.g. HV toggled on the front panel), so the app re-announces
        the descriptor and the config UIs (local + remote) reflect the real state."""
        self._sink_dirty = True

    def take_sink_dirty(self) -> bool:
        """True (once) if a sink's readback value changed since the last check — the
        app polls active devices on its refresh tick and re-announces if any did."""
        dirty, self._sink_dirty = self._sink_dirty, False
        return dirty

    # -- sinks (control) ------------------------------------------------------
    def write(self, sink_id: str, value=None) -> None:
        schema = self._sink_schema(sink_id)
        if schema is None:
            raise KeyError(f"no sink {sink_id!r} on {self._instance_id}")
        if schema.kind != SinkKind.ACTION:
            value = self._coerce(schema, value)
        with self._guard():                  # serialized with _read/_connect
            self._write(schema, value)
        if schema.kind != SinkKind.ACTION:
            self._sink_values[sink_id] = value

    def set_rate_hz(self, hz: float) -> None:
        if self._rate is None or self._rate.mode != RateMode.SETTABLE:
            return
        lo = self._rate.min_hz if self._rate.min_hz is not None else 1e-3
        hi = self._rate.max_hz if self._rate.max_hz is not None else float(hz)
        self._rate_hz = max(lo, min(hi, float(hz)))

    # -- configuration --------------------------------------------------------
    def set_option(self, key: str, value) -> None:
        for o in self._options:
            if o.key == key:
                if o.choices and value not in [c[0] for c in o.choices]:
                    return
                self._option_values[key] = value
                self._on_option(key, value)
                return

    def _on_option(self, key: str, value) -> None:
        """Hook: react to an option change (e.g. reconfigure hardware)."""

    def check(self) -> CheckResult:
        """Connect (if needed) and report the source count — a generic "is it
        working?" probe for the config GUI. Drivers with auth / remote endpoints
        override this for a precise message (which is why it lives on the contract)."""
        try:
            with self._guard():
                self._connect()
        except Exception as exc:                       # noqa: BLE001
            return CheckResult(False, f"Connection failed: {exc}")
        n = len(self._sources)
        return CheckResult(True, f"Connected · {n} source{'' if n == 1 else 's'}.", n)

    def _sink_schema(self, sink_id: str) -> Optional[Sink]:
        for s in self._sinks:
            if s.id == sink_id:
                return s
        return None

    @staticmethod
    def _coerce(schema: Sink, value):
        if schema.kind == SinkKind.TOGGLE:
            return bool(value)
        if schema.kind == SinkKind.ENUM:
            options = schema.params[0].options if schema.params else ()
            if options and value not in options:
                raise ValueError(f"{value!r} not in {options}")
            return value
        if schema.kind == SinkKind.SETPOINT:
            v = float(value)
            p = schema.params[0] if schema.params else None
            if p is not None:
                if p.minimum is not None:
                    v = max(p.minimum, v)
                if p.maximum is not None:
                    v = min(p.maximum, v)
            return v
        return value

    # -- data plane (push) ----------------------------------------------------
    def start(self, emit) -> None:
        if self._streaming:
            return
        self._emit = emit
        self._streaming = True
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"poll-{self._instance_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._streaming = False
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._emit = None

    # -- device → tag channel (DESIGN §7.3) ---------------------------------- #
    def set_tag_sink(self, sink) -> None:
        """Platform hook: install the ``callable(Marker)`` that carries this device's
        emitted tags to the app's TagStore. Injected by the DeviceManager when the
        device goes active (the tag analogue of the ``start(emit)`` reading sink);
        ``None`` disables emission. A driver never sets this itself."""
        self._tag_sink = sink

    def emit_tag(self, marker) -> None:
        """Raise a device-origin tag — an alarm, event or gas-detected crossing (§7.3).
        A driver builds a :class:`Marker` (origin=device) and calls this; it forwards to
        the platform-injected sink, and is a safe no-op until one is wired. Runs on the
        acquisition (poll) thread — the sink marshals to the GUI thread."""
        sink = self._tag_sink
        if sink is not None:
            try:
                sink(marker)
            except Exception:              # a tag must never break acquisition
                pass

    # -- device → app → device request/response channel ---------------------- #
    def set_prompt_sink(self, sink) -> None:
        """Platform hook: install the ``callable(Prompt, on_response)`` that carries this
        device's operator REQUESTS to the app's PendingInteractions store. Injected by the
        DeviceManager when the device goes active (the request/response analogue of the
        tag sink); ``None`` disables it. A driver never sets this itself."""
        self._prompt_sink = sink

    def ask(self, prompt, on_response=None) -> None:
        """Raise a device-initiated REQUEST the operator must answer before the device
        proceeds — "Have you retracted the arm? [Yes]/[No]" (the fourth primitive, see
        core.interaction). A driver builds a :class:`Prompt` and calls this with an
        ``on_response(answer)`` callback (the driver then, e.g., sends an ack command);
        it forwards to the platform-injected sink and is a safe no-op until one is wired.
        Runs on the acquisition/reader thread — the sink marshals to the GUI thread, and
        the store invokes ``on_response`` there (once, first-responder-wins)."""
        sink = self._prompt_sink
        if sink is not None:
            try:
                sink(prompt, on_response)
            except Exception:              # a prompt must never break acquisition
                pass

    def set_prompt_withdraw_sink(self, sink) -> None:
        """Platform hook: install the ``callable(prompt_id)`` that RETIRES a device-raised
        request from the app store — the DEVICE resolved it itself (its front panel / another
        transport answered first, first-responder-wins), so it must leave the inbox WITHOUT
        this app answering it. Injected by the DeviceManager like the prompt sink; ``None``
        disables it. A driver never sets this itself."""
        self._prompt_withdraw_sink = sink

    def withdraw_prompt(self, prompt_id) -> None:
        """Retire a request THIS device raised via :meth:`ask`, because the device resolved it
        locally (its ``?DONE`` / an answer on the instrument). Drops it from the app's inbox
        WITHOUT answering it — no double-answer. Runs on the reader thread; the sink marshals
        to the GUI thread. A safe no-op until wired."""
        sink = self._prompt_withdraw_sink
        if sink is not None:
            try:
                sink(prompt_id)
            except Exception:              # a withdrawal must never break acquisition
                pass

    def _poll_loop(self) -> None:
        while self._streaming:
            cycle = time.monotonic()
            now = time.time()
            emit = self._emit
            for src in self._sources:
                try:
                    with self._guard():         # serialized with writes/reconnect
                        value, status = self._read(src)
                except Exception:
                    value, status = float("nan"), 1
                if emit is not None:
                    emit(Reading(self.data_id, src.id, now, value, status))
            self._supervise()                   # auto-reconnect a dropped link (opt-in, §21.1)
            interval = 1.0 / (self._rate_hz or 1.0)
            remaining = interval - (time.monotonic() - cycle)
            while self._streaming and remaining > 0:
                chunk = min(remaining, 0.05)
                time.sleep(chunk)
                remaining -= chunk

    # -- reconnect supervisor (DESIGN §21.1) ---------------------------------- #
    def _supervise(self) -> None:
        """Auto-reconnect a dropped link, once per poll cycle. OPT-IN: a no-op unless the
        driver sets ``reconnectable`` (its _connect/_disconnect genuinely reopen a physical
        link with no destructive side effects). Keys ONLY on the driver's ``_link_healthy()``
        verdict — a transport FAILURE, never a value — so a channel that legitimately reads
        NaN can't trip it. On a confirmed dead link: status → ERROR, run the low-level
        ``_disconnect`` (frees the port lock + arbiter), then reconnect on a throttled backoff
        while the port is present, and resume on success. Runs on the poll thread."""
        if not self.reconnectable:
            return
        if self._status == Status.ERROR:                 # -- reconnect phase --
            if self._port_present() and self._throttle("reconnect", self.reconnect_backoff):
                try:
                    with self._guard():
                        self._connect()
                except Exception as exc:                 # noqa: BLE001 — stay ERROR, retry later
                    self._last_error = str(exc)
                else:
                    self._status = Status.CONNECTED
                    self._last_error = None
            return
        if self._link_healthy() is False:                # -- the driver reports the link DOWN --
            self._status = Status.ERROR
            self._last_error = "link lost — auto-reconnecting"
            try:
                with self._guard():
                    self._disconnect()
            except Exception:                            # noqa: BLE001
                pass

    def _link_healthy(self):
        """A ``reconnectable`` driver's verdict on its link for the supervisor: ``False`` = it
        is DOWN (a transport failure — an exception / a hard link-down flag, NOT a value),
        ``True`` = alive, ``None`` = no opinion (the default; the device is never
        auto-reconnected). Only serial-transport failures should ever return ``False``."""
        return None

    def _port_present(self) -> bool:
        """Whether the device's physical port still exists — the supervisor won't hammer a
        reconnect at a physically-removed device. Default ``True``; serial drivers override
        to check ``serial.tools.list_ports.comports()``."""
        return True

    # -- hooks for subclasses -------------------------------------------------
    def _connect(self) -> None: ...

    def _disconnect(self) -> None: ...

    def _write(self, sink: Sink, value) -> None:
        """Send the value to hardware. Default no-op (store-only)."""

    def _read(self, source: Source):
        """Read one source: return ``(value, status)``. Override in drivers."""
        raise NotImplementedError(f"{self.driver} has no _read()")
