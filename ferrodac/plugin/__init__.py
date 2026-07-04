"""ferroDAC plugin SDK — the STABLE, versioned surface third-party extensions code
against.

Import ONLY from here — a driver, processor, or widget written against this facade
keeps working across releases (gated by ``API_VERSION`` / a manifest's ``api``)::

    # a device driver
    from ferrodac.plugin import (BaseDevice, Source, Sink, SinkKind, Param, Option,
                                 Interface, Modality, RateControl, RateMode, Status,
                                 CheckResult)
    # a processor / a widget
    from ferrodac.plugin import Processor, Port, Widget, Trace, FLOAT, BOOL, TRACE

Everything behind this facade may change between releases; this module is the one
contract we promise to keep stable. It only grows (additively), so a plugin that
targets an older ``api`` still loads.

The driver/processor vocabulary is Qt-free, so a driver- or processor-only plugin
never pulls in Qt. ``Widget`` (a QWidget) + the device-config panel are imported
lazily, only when referenced.

Threading (DESIGN §21.1 — READ THIS): the app disables cyclic GC and collects it
from the GUI thread, so **QObjects are created, used and destroyed on the GUI thread
only.** Consequently:

- ``Widget.feed`` always runs on the GUI thread (safe to touch Qt).
- ``Processor.process`` runs on a platform WORKER thread by default (so a slow
  analysis can never freeze the app) — it must be Qt-free. A processor that must run
  on the GUI thread sets the class attribute ``requires_gui = True``.
- A ``BaseDevice`` driver's ``_read``/``_connect``/``_write`` run on the device's own
  acquisition/config threads (never the GUI thread) — also Qt-free. The platform
  **serializes them per device** (``self._io_lock``), so a driver never needs its own
  lock; ``self._throttle(key, interval)`` rate-limits reconnects. Serial drivers share
  one process-wide port registry so two can't open the same port.

Never construct or touch a QObject from any of those; marshal to the GUI via a
signal if you must. Violations are printed (with the offending thread + stack) by the
diagnostics message handler.
"""

# The API is additive-only; bump on each release that adds to this surface. A
# manifest's `api` must be <= this (see manifest.is_compatible). v2 added the full
# device-driver vocabulary (BaseDevice + the descriptor types) and the documented
# threading contract (Processor.requires_gui).
API_VERSION = 2

# The closed datatype vocabulary that flows source → processor → widget. `trace` is a
# 1-D labelled array (see Trace), interoperable with xarray/pint; new types are added
# only via core releases.
FLOAT = "float"
BOOL = "bool"
TRACE = "trace"
DTYPES = frozenset({FLOAT, BOOL, TRACE})

from ..analysis.processor import Port, Processor   # noqa: E402 — Qt-free contract
from ..analysis.processor import register as register_processor  # noqa: E402
from ..core.base import BaseDevice                 # noqa: E402 — Qt-free driver base
from ..core.device import (                        # noqa: E402 — Qt-free contract
    CheckResult,
    Device,
    DeviceDescriptor,
    Interface,
    Modality,
    Option,
    Param,
    RateControl,
    RateMode,
    Sink,
    SinkKind,
    Source,
    Status,
)
from ..core.trace import Trace                     # noqa: E402 — Qt-free contract

# Device drivers register simply by subclassing BaseDevice (auto-discovered);
# processors and widgets register with the decorators. A driver can also ship a
# dedicated config panel (DeviceConfigWidget + @register_config_widget) — Qt, so
# lazily exposed below.
__all__ = ["API_VERSION", "FLOAT", "BOOL", "TRACE", "DTYPES",
           "Port", "Processor", "Trace", "CheckResult",
           # device-driver vocabulary (v2)
           "BaseDevice", "Device", "DeviceDescriptor", "Source", "Sink", "SinkKind",
           "Param", "Option", "Interface", "Modality", "RateControl", "RateMode",
           "Status",
           # Qt surfaces (lazy)
           "Widget", "DeviceConfigWidget",
           "register_processor", "register_widget", "register_config_widget"]


def __getattr__(name):
    """Lazily expose the Qt-touching names (Widget + the device-config panel surface)
    so a processor/driver-only plugin that never references them stays Qt-free."""
    if name == "Widget":
        from ..ui.widget import Widget
        return Widget
    if name == "register_widget":
        from ..ui.widget import register_widget
        return register_widget
    if name == "DeviceConfigWidget":
        from ..ui.device_config import DeviceConfigWidget
        return DeviceConfigWidget
    if name == "register_config_widget":
        from ..ui.device_config import register_config_widget
        return register_config_widget
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
