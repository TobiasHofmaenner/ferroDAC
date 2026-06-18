"""Engine — the Qt wrapper around the data-plane Bus.

The publish/subscribe/batch-drain mechanics live in the Qt-free `core.bus.Bus`
(so replay/compute can use them headlessly, DESIGN §4.1). The Engine just owns a
Bus, **pumps `drain()` from a `QTimer`** on the GUI thread, emits `tick`, and
manages device lifecycle — so the consume/repaint rate stays decoupled from the
sample rate.

This is the one door everything hangs off:
  - the live cards subscribe (via the `tick` signal + `latest()`),
  - the chart subscribes as a sink,
  - the CSV/store/network sinks and user scripts register here too.
"""

from __future__ import annotations

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QObject, QTimer, Signal

from .bus import Bus
from .reading import Reading


class Engine(QObject):
    #: emitted on the GUI thread after each drain (consumers may read latest())
    tick = Signal()

    def __init__(self, drain_ms: int = 50, parent=None):
        super().__init__(parent)
        self._bus = Bus()
        self._devices: dict[str, object] = {}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(drain_ms)

    @property
    def bus(self) -> Bus:
        return self._bus

    # -- bus delegation (same public API as before) --------------------------
    def publish(self, reading: Reading) -> None:
        self._bus.publish(reading)

    def subscribe(self, sink):
        return self._bus.subscribe(sink)

    def latest(self) -> dict:
        return self._bus.latest()

    # -- device streaming ----------------------------------------------------
    def start_device(self, device) -> None:
        self._devices[device.instance_id] = device
        device.start(self._ingest)

    def stop_device(self, device) -> None:
        self._devices.pop(device.instance_id, None)
        try:
            device.stop()
        except Exception:
            pass

    def _ingest(self, reading: Reading) -> None:
        """Called from a device's acquisition thread — must stay cheap & safe."""
        self._bus.publish(reading)

    # -- drain (GUI thread): pump the bus, then notify -----------------------
    def _drain(self) -> None:
        if self._bus.drain():            # fans out to sinks + updates latest
            self.tick.emit()

    def shutdown(self) -> None:
        self._timer.stop()
        for d in list(self._devices.values()):
            try:
                d.stop()
            except Exception:
                pass
