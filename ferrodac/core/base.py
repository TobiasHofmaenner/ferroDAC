"""BaseDevice — convenience base implementing the common Device machinery.

Drivers usually subclass this: it holds the descriptor fields, a status
state-machine, current sink values, the configured sample rate, and the
acquisition loop. A driver implements `discover` + `_connect`/`_disconnect`
(+ `_read` for sources, `_write` for sinks).
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Optional, Sequence

from .reading import Reading
from .device import (
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


class BaseDevice(Device):
    driver = "base"   # registry skips this; real drivers override

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
    ):
        self._instance_id = instance_id
        self._name = name
        self._interface = interface
        self._sources = list(sources)
        self._sinks = list(sinks)
        self._options = list(options)
        self._option_values = {o.key: o.value for o in self._options}
        self._rate = rate
        self._primary_source = primary_source
        self._hardware_id = hardware_id
        self._model = model
        self._firmware: Optional[str] = None
        self._status = Status.DISCOVERED
        self._last_error: Optional[str] = None

        self._sink_values = {
            s.id: s.value for s in self._sinks if s.kind != SinkKind.ACTION
        }
        self._rate_hz = rate.default_hz if rate else None

        self._streaming = False
        self._thread: Optional[threading.Thread] = None
        self._emit = None

    # -- identity / description ----------------------------------------------
    @property
    def instance_id(self) -> str:
        return self._instance_id

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
            driver=self.driver,
            name=self._name,
            interface=self._interface,
            status=self._status,
            hardware_id=self._hardware_id,
            model=self._model,
            firmware=self._firmware,
            sources=list(self._sources),
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

    def connect(self) -> None:
        self._status = Status.CONNECTING
        self._last_error = None
        try:
            self._connect()
            self._status = Status.CONNECTED
        except Exception as exc:
            self._status = Status.ERROR
            self._last_error = str(exc)
            raise

    def disconnect(self) -> None:
        self.stop()
        try:
            self._disconnect()
        finally:
            self._status = Status.DISCONNECTED

    # -- sinks (control) ------------------------------------------------------
    def write(self, sink_id: str, value=None) -> None:
        schema = self._sink_schema(sink_id)
        if schema is None:
            raise KeyError(f"no sink {sink_id!r} on {self._instance_id}")
        if schema.kind != SinkKind.ACTION:
            value = self._coerce(schema, value)
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

    def _poll_loop(self) -> None:
        while self._streaming:
            cycle = time.monotonic()
            now = time.time()
            emit = self._emit
            for src in self._sources:
                try:
                    value, status = self._read(src)
                except Exception:
                    value, status = float("nan"), 1
                if emit is not None:
                    emit(Reading(self._instance_id, src.id, now, value, status))
            interval = 1.0 / (self._rate_hz or 1.0)
            remaining = interval - (time.monotonic() - cycle)
            while self._streaming and remaining > 0:
                chunk = min(remaining, 0.05)
                time.sleep(chunk)
                remaining -= chunk

    # -- hooks for subclasses -------------------------------------------------
    def _connect(self) -> None: ...

    def _disconnect(self) -> None: ...

    def _write(self, sink: Sink, value) -> None:
        """Send the value to hardware. Default no-op (store-only)."""

    def _read(self, source: Source):
        """Read one source: return ``(value, status)``. Override in drivers."""
        raise NotImplementedError(f"{self.driver} has no _read()")
