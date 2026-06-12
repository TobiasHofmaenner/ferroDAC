"""BaseSource — a convenience base implementing the common Source machinery.

Drivers usually subclass this rather than :class:`Source` directly: it holds the
descriptor fields, a small status state-machine, current control values, and the
configured sample rate. A driver only has to build its channels/controls and
implement ``_connect`` / ``_disconnect`` (and ``_invoke`` if it has controls).
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Optional, Sequence

from .reading import Reading
from .source import (
    Channel,
    Control,
    ControlKind,
    Interface,
    RateControl,
    RateMode,
    Source,
    SourceDescriptor,
    Status,
)


class BaseSource(Source):
    driver = "base"   # registry skips this; real drivers override

    def __init__(
        self,
        instance_id: str,
        name: str,
        interface: Interface,
        channels: Sequence[Channel] = (),
        controls: Sequence[Control] = (),
        rate: Optional[RateControl] = None,
        primary_channel: Optional[str] = None,
        hardware_id: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._instance_id = instance_id
        self._name = name
        self._interface = interface
        self._channels = list(channels)
        self._controls = list(controls)
        self._rate = rate
        self._primary_channel = primary_channel
        self._hardware_id = hardware_id
        self._model = model
        self._firmware: Optional[str] = None
        self._status = Status.DISCOVERED
        self._last_error: Optional[str] = None

        # current value of every non-action control (seeded from its declared value)
        self._control_values = {
            c.id: c.value for c in self._controls if c.kind != ControlKind.ACTION
        }
        # currently configured sample rate
        self._rate_hz = rate.default_hz if rate else None

        # streaming state (data plane)
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
        """In-memory display rename (persistence comes with workspaces)."""
        self._name = name

    @property
    def status(self) -> Status:
        return self._status

    def describe(self) -> SourceDescriptor:
        controls = []
        for c in self._controls:
            if c.kind == ControlKind.ACTION:
                controls.append(c)
            else:
                controls.append(replace(c, value=self._control_values.get(c.id, c.value)))
        return SourceDescriptor(
            instance_id=self._instance_id,
            driver=self.driver,
            name=self._name,
            interface=self._interface,
            status=self._status,
            hardware_id=self._hardware_id,
            model=self._model,
            firmware=self._firmware,
            channels=list(self._channels),
            controls=controls,
            rate=self._rate,
            rate_hz=self._rate_hz,
            primary_channel=self._primary_channel,
            last_error=self._last_error,
        )

    # -- lifecycle (template methods) ----------------------------------------
    def mark_connecting(self) -> None:
        """Optimistically flip to CONNECTING (so the UI shows it before the
        blocking `connect()` runs on a worker)."""
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
        """The single acquisition loop. Reads every channel each tick at the
        configured rate and pushes a Reading per channel."""
        while self._streaming:
            cycle = time.monotonic()
            now = time.time()
            emit = self._emit
            for ch in self._channels:
                try:
                    value, status = self._read(ch)
                except Exception:
                    value, status = float("nan"), 1
                if emit is not None:
                    emit(Reading(self._instance_id, ch.id, now, value, status))
            interval = 1.0 / (self._rate_hz or 1.0)
            remaining = interval - (time.monotonic() - cycle)
            while self._streaming and remaining > 0:
                chunk = min(remaining, 0.05)
                time.sleep(chunk)
                remaining -= chunk

    # -- control / configuration ---------------------------------------------
    def invoke(self, control_id: str, value=None) -> None:
        schema = self._control_schema(control_id)
        if schema is None:
            raise KeyError(f"no control {control_id!r} on {self._instance_id}")
        if schema.kind != ControlKind.ACTION:
            value = self._coerce(schema, value)
        self._invoke(schema, value)              # hardware hook
        if schema.kind != ControlKind.ACTION:
            self._control_values[control_id] = value

    def set_rate_hz(self, hz: float) -> None:
        if self._rate is None or self._rate.mode != RateMode.SETTABLE:
            return
        lo = self._rate.min_hz if self._rate.min_hz is not None else 1e-3
        hi = self._rate.max_hz if self._rate.max_hz is not None else float(hz)
        self._rate_hz = max(lo, min(hi, float(hz)))

    def _control_schema(self, control_id: str) -> Optional[Control]:
        for c in self._controls:
            if c.id == control_id:
                return c
        return None

    @staticmethod
    def _coerce(schema: Control, value):
        """Validate/coerce a value against the control schema."""
        if schema.kind == ControlKind.TOGGLE:
            return bool(value)
        if schema.kind == ControlKind.ENUM:
            options = schema.params[0].options if schema.params else ()
            if options and value not in options:
                raise ValueError(f"{value!r} not in {options}")
            return value
        if schema.kind == ControlKind.SETPOINT:
            v = float(value)
            p = schema.params[0] if schema.params else None
            if p is not None:
                if p.minimum is not None:
                    v = max(p.minimum, v)
                if p.maximum is not None:
                    v = min(p.maximum, v)
            return v
        return value

    # -- hooks for subclasses -------------------------------------------------
    def _connect(self) -> None:
        """Confirm/open the device; may set self._firmware etc. Override."""

    def _disconnect(self) -> None:
        """Release the device. Override."""

    def _invoke(self, control: Control, value) -> None:
        """Send the control to the hardware. Default no-op (store-only).
        Real drivers override to talk to the device."""

    def _read(self, channel: Channel):
        """Read one channel: return ``(value, status)``. Override in drivers."""
        raise NotImplementedError(f"{self.driver} has no _read()")
