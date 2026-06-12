"""BaseSource — a convenience base implementing the common Source machinery.

Drivers usually subclass this rather than :class:`Source` directly: it holds the
descriptor fields + a small status state-machine, so a driver only has to build
its channels/controls and implement `_connect()` / `_disconnect()`.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .source import (
    Channel,
    Control,
    Interface,
    RateControl,
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
            controls=list(self._controls),
            rate=self._rate,
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
        try:
            self._disconnect()
        finally:
            self._status = Status.DISCONNECTED

    # -- hooks for subclasses -------------------------------------------------
    def _connect(self) -> None:
        """Confirm/open the device; may set self._firmware etc. Override."""

    def _disconnect(self) -> None:
        """Release the device. Override."""
