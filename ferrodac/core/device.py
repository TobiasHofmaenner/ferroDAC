"""The **Device contract** — the load-bearing interface of ferroDAC.

Vocabulary (signal-flow):
  - a **Device** is an instrument (a driver instance).
  - a **Source** is a data-output endpoint on a device (it produces data).
  - a **Sink** is a control-input endpoint on a device (it consumes a value).

The UI/orchestrator bind to a serializable **DeviceDescriptor**, never to the
Device object. Discovery is a type-level capability; identity + lifecycle are
instance-level. The data plane (start/stop) pushes Readings from a device's
Sources; writes go to its Sinks.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
#  Enumerations
# --------------------------------------------------------------------------- #
class Modality(enum.Enum):
    SCALAR = "scalar"
    WAVEFORM = "waveform"
    IMAGE = "image"
    VIDEO = "video"
    STATUS = "status"


class Status(enum.Enum):
    DISCOVERED = "discovered"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class SinkKind(enum.Enum):
    ACTION = "action"        # no value (zero, degas, reset)
    SETPOINT = "setpoint"    # a typed value
    TOGGLE = "toggle"        # on / off
    ENUM = "enum"            # one of a set of options


class RateMode(enum.Enum):
    FIXED = "fixed"
    SETTABLE = "settable"
    DECIMATE_ONLY = "decimate_only"


# --------------------------------------------------------------------------- #
#  Descriptor value objects (serializable)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Param:
    name: str
    dtype: str = "float64"
    unit: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    options: tuple = ()


@dataclass(frozen=True)
class Option:
    """A device-level configuration choice (e.g. a camera's capture format).

    Distinct from a Sink: it is not routable, it just parameterises the device.
    ``choices`` is a tuple of ``(value, label)``; ``value`` is the current value.
    """
    key: str
    name: str
    choices: tuple = ()
    value: object = None


@dataclass(frozen=True)
class Source:
    """A data-output endpoint on a device (produces data)."""
    id: str
    name: str
    unit: str = ""
    modality: Modality = Modality.SCALAR
    dtype: str = "float"
    prefer_log: bool = False


@dataclass(frozen=True)
class Sink:
    """A control-input endpoint on a device (consumes a value).

    Schema (id/name/kind/params) is declared by the driver; `value` is the
    current value, filled into the snapshot (None for ACTIONs).
    """
    id: str
    name: str
    kind: SinkKind = SinkKind.ACTION
    params: tuple = ()
    required_permission: str = "command"   # reserved for RBAC
    value: object = None


@dataclass(frozen=True)
class Interface:
    kind: str                        # modbus_rtu | rs232 | rs485 | tcp | usb | sim
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RateControl:
    mode: RateMode = RateMode.FIXED
    native_hz: Optional[float] = None
    default_hz: Optional[float] = None
    min_hz: Optional[float] = None
    max_hz: Optional[float] = None


@dataclass
class DeviceDescriptor:
    """A snapshot of a device's identity, capabilities and status."""
    instance_id: str
    driver: str
    name: str
    interface: Interface
    uuid: Optional[str] = None   # data-plane identity (None until onboarded)
    status: Status = Status.DISCOVERED
    hardware_id: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    sources: list = field(default_factory=list)   # list[Source]
    sinks: list = field(default_factory=list)      # list[Sink]
    options: list = field(default_factory=list)    # list[Option]
    rate: Optional[RateControl] = None
    rate_hz: Optional[float] = None
    primary_source: Optional[str] = None
    last_error: Optional[str] = None

    @property
    def primary(self) -> Optional[Source]:
        if self.primary_source:
            for s in self.sources:
                if s.id == self.primary_source:
                    return s
        if len(self.sources) == 1:
            return self.sources[0]
        return None


# --------------------------------------------------------------------------- #
#  The Device contract
# --------------------------------------------------------------------------- #
class Device(ABC):
    """Base contract for every device. Drivers usually extend
    :class:`ferrodac.core.base.BaseDevice`."""

    driver: str = "device"
    discoverable: bool = False

    @classmethod
    def discover(cls) -> list["Device"]:
        """Return instances currently found on the system (cheap enumeration)."""
        return []

    @property
    @abstractmethod
    def instance_id(self) -> str:
        """Stable key used to dedup across scans and to address the device."""

    @abstractmethod
    def describe(self) -> DeviceDescriptor:
        """A fresh snapshot of identity + capabilities + status."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    # -- sinks (control) ------------------------------------------------------
    def write(self, sink_id: str, value=None) -> None:
        """Write to a sink: trigger an ACTION or set a value. Implemented by
        BaseDevice; devices with no sinks never receive this."""
        raise NotImplementedError(f"{self.driver} exposes no writable sinks")

    # -- configuration --------------------------------------------------------
    def set_option(self, key: str, value) -> None:
        """Set a device configuration option (see DeviceDescriptor.options)."""
        raise NotImplementedError(f"{self.driver} exposes no options")

    # -- data plane (push) ----------------------------------------------------
    def start(self, emit) -> None:
        """Begin streaming: call ``emit(reading)`` for each sample."""
        raise NotImplementedError(f"{self.driver} does not stream")

    def stop(self) -> None:
        raise NotImplementedError(f"{self.driver} does not stream")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.instance_id!r}>"
