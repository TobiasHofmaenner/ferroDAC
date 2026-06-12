"""The **Source contract** — the load-bearing interface of ferroDAC.

A *Source* is a producer of data: a device, a camera, a custom process. This
module defines:

  - the serializable **SourceDescriptor** (+ Channel, Control, Interface,
    RateControl) that a source advertises to the orchestrator/UI, and
  - the **Source** ABC that every driver implements.

Design contract (agreed in the design phase — see docs/DESIGN.md):

  - **The UI/orchestrator bind to the descriptor, never to the Source object.**
    In v1 the descriptor is an in-process dataclass; later the *same* shape
    serialises over the wire to a remote UI, unchanged.
  - **Discovery is a type-level capability** (`discoverable` / `discover()`);
    **identity + lifecycle are instance-level**.
  - **Progressive population:** discovery yields identity + interface (cheap);
    `connect()` enriches firmware + confirmed channels/controls + status.
  - **Reserved slots** for the data plane (`start`/`stop`/`invoke`) are declared
    but not implemented in v1 — the ABC only *gains* methods later, never
    restructures.
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


class ControlKind(enum.Enum):
    ACTION = "action"        # no value (zero, degas, reset)
    SETPOINT = "setpoint"    # a typed value
    TOGGLE = "toggle"        # on / off
    ENUM = "enum"            # one of a set of options


class RateMode(enum.Enum):
    FIXED = "fixed"                  # the device dictates the rate
    SETTABLE = "settable"            # platform/driver can set within [min, max]
    DECIMATE_ONLY = "decimate_only"  # device streams fast; driver can downsample


# --------------------------------------------------------------------------- #
#  Descriptor value objects (serializable)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Param:
    """A typed parameter for a control."""
    name: str
    dtype: str = "float64"
    unit: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    options: tuple = ()              # for ENUM controls


@dataclass(frozen=True)
class Channel:
    """One readable signal from a source ("a topic, incl. datatype")."""
    id: str
    name: str
    unit: str = ""
    modality: Modality = Modality.SCALAR
    dtype: str = "float64"
    prefer_log: bool = False         # plotting hint (used later)


@dataclass(frozen=True)
class Control:
    """A writable operation (a command or a setting).

    The schema (id/name/kind/params) is declared by the driver; `value` is the
    *current* value, filled into the snapshot by the source (None for ACTIONs).
    """
    id: str
    name: str
    kind: ControlKind = ControlKind.ACTION
    params: tuple = ()               # tuple[Param, ...]
    required_permission: str = "command"   # reserved for RBAC
    value: object = None             # current value snapshot


@dataclass(frozen=True)
class Interface:
    """How the source connects ("the device interface")."""
    kind: str                        # modbus_rtu | rs232 | rs485 | tcp | usb | sim
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RateControl:
    """How sampling rate is controlled for this source (see DESIGN Q2)."""
    mode: RateMode = RateMode.FIXED
    native_hz: Optional[float] = None
    default_hz: Optional[float] = None
    min_hz: Optional[float] = None
    max_hz: Optional[float] = None


@dataclass
class SourceDescriptor:
    """A snapshot of a source's identity, capabilities and status.

    This is the only thing the UI/orchestrator see. `describe()` returns a fresh
    snapshot reflecting current state.
    """
    instance_id: str                 # stable dedup/address key (always present)
    driver: str                      # which Source subclass produced it
    name: str                        # display name, user-overridable
    interface: Interface
    status: Status = Status.DISCOVERED
    hardware_id: Optional[str] = None    # read from device if possible
    model: Optional[str] = None
    firmware: Optional[str] = None
    channels: list = field(default_factory=list)   # list[Channel] (after connect)
    controls: list = field(default_factory=list)   # list[Control] (declared, w/ values)
    rate: Optional[RateControl] = None             # rate capability
    rate_hz: Optional[float] = None                # currently configured rate
    primary_channel: Optional[str] = None          # id of the headline channel
    last_error: Optional[str] = None

    @property
    def primary(self) -> Optional[Channel]:
        """The channel to feature on the card — deterministically.

        Explicit `primary_channel` wins; a single-channel source auto-features
        its only channel; otherwise None.
        """
        if self.primary_channel:
            for ch in self.channels:
                if ch.id == self.primary_channel:
                    return ch
        if len(self.channels) == 1:
            return self.channels[0]
        return None


# --------------------------------------------------------------------------- #
#  The Source contract
# --------------------------------------------------------------------------- #
class Source(ABC):
    """Base contract for every data source.

    Subclasses are auto-registered by the registry (see registry.py). Most
    drivers will extend :class:`ferrodac.core.base.BaseSource`, which implements
    the common state machine; this ABC is the minimal contract.
    """

    #: Human-readable driver/type id, e.g. "tpg256a". Real drivers override.
    driver: str = "source"

    #: Whether this source *type* can scan for available instances itself.
    discoverable: bool = False

    # -- type-level discovery -------------------------------------------------
    @classmethod
    def discover(cls) -> list["Source"]:
        """Return instances currently found on the system.

        Cheap enumeration only — confirming handshakes happen in `connect()`.
        Must be safe to call repeatedly (the manager scans periodically and
        dedups by `instance_id`). Override in discoverable sources.
        """
        return []

    # -- instance identity & description -------------------------------------
    @property
    @abstractmethod
    def instance_id(self) -> str:
        """Stable key used to dedup across scans and to address the source."""

    @abstractmethod
    def describe(self) -> SourceDescriptor:
        """A fresh snapshot of identity + capabilities + status."""

    # -- lifecycle ------------------------------------------------------------
    @abstractmethod
    def connect(self) -> None:
        """Open/confirm the device, enrich the descriptor, set status."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the device."""

    # -- control / configuration ---------------------------------------------
    def invoke(self, control_id: str, value=None) -> None:
        """Invoke a declared control: trigger an ACTION or set a value.

        Implemented by :class:`~ferrodac.core.base.BaseSource`. Sources that
        declare no controls never receive this.
        """
        raise NotImplementedError(f"{self.driver} exposes no invokable controls")

    # -- data plane (push) ----------------------------------------------------
    def start(self, emit) -> None:
        """Begin streaming: call ``emit(reading)`` for every sample.

        The source owns the *one* acquisition loop (poll-type drivers run an
        internal timer at the configured rate; streamer-type forward what the
        device pushes). Implemented by BaseSource.
        """
        raise NotImplementedError(f"{self.driver} does not stream")

    def stop(self) -> None:
        """Stop streaming."""
        raise NotImplementedError(f"{self.driver} does not stream")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<{type(self).__name__} {self.instance_id!r}>"
