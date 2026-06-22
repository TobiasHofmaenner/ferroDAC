"""ferroDAC plugin SDK — the STABLE, versioned surface third-party extensions code
against.

Import ONLY from here::

    from ferrodac.plugin import Processor, Port, Device, Widget, Trace, FLOAT, BOOL, TRACE

Everything behind this facade may change between releases; this module is the one
contract we promise to keep stable (gated by ``API_VERSION`` / a manifest's ``api``).

Processor/Port/Device/Trace are Qt-free, so a processor- or driver-only plugin never
pulls in Qt. ``Widget`` (a QWidget) is imported lazily, only when referenced.
"""

API_VERSION = 1

# The closed datatype vocabulary that flows source → processor → widget. `trace` is a
# 1-D labelled array (see Trace), interoperable with xarray/pint; new types are added
# only via core releases.
FLOAT = "float"
BOOL = "bool"
TRACE = "trace"
DTYPES = frozenset({FLOAT, BOOL, TRACE})

from ..analysis.processor import Port, Processor   # noqa: E402 — Qt-free contract
from ..analysis.processor import register as register_processor  # noqa: E402
from ..core.device import Device                   # noqa: E402 — Qt-free contract
from ..core.trace import Trace                     # noqa: E402 — Qt-free contract

# Device drivers register simply by subclassing Device (auto-discovered); processors
# and widgets register with these decorators.
__all__ = ["API_VERSION", "FLOAT", "BOOL", "TRACE", "DTYPES",
           "Port", "Processor", "Device", "Trace", "Widget",
           "register_processor", "register_widget"]


def __getattr__(name):
    """Lazily expose the Qt-touching names (Widget + register_widget) so a
    processor/driver-only plugin that never references them stays Qt-free."""
    if name == "Widget":
        from ..ui.widget import Widget
        return Widget
    if name == "register_widget":
        from ..ui.widget import register_widget
        return register_widget
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
