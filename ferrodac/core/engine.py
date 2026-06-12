"""Engine — the data-plane hub.

Sources push Readings into the engine from their acquisition threads. The engine
buffers them and, on a timer (the GUI thread), **drains the buffer in batches**
and fans them out to registered **sinks** — so the consume/repaint rate is fully
decoupled from the sample rate.

This is the one door everything hangs off:
  - the live cards subscribe (via the `tick` signal + `latest()`),
  - the chart subscribes as a sink,
  - later, the CSV/network sinks and **user scripts** register here too.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QObject, QTimer, Signal

from .reading import Reading

Sink = Callable[[list], None]  # Callable[[list[Reading]], None]


class Engine(QObject):
    #: emitted on the GUI thread after each drain (consumers may read latest())
    tick = Signal()

    def __init__(self, drain_ms: int = 50, parent=None):
        super().__init__(parent)
        self._devices: dict[str, object] = {}
        self._inbox: deque = deque()        # thread-safe append / popleft
        self._latest: dict[str, Reading] = {}
        self._sinks: list[Sink] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(drain_ms)

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
        self._inbox.append(reading)

    def publish(self, reading: Reading) -> None:
        """Inject a reading from a non-device source (e.g. a virtual UI source)."""
        self._inbox.append(reading)

    # -- sinks ---------------------------------------------------------------
    def subscribe(self, sink: Sink) -> Callable[[], None]:
        """Register a sink (called on the GUI thread with a batch of Readings).
        Returns an unsubscribe callable."""
        self._sinks.append(sink)

        def _unsub():
            if sink in self._sinks:
                self._sinks.remove(sink)

        return _unsub

    def latest(self) -> dict[str, Reading]:
        return dict(self._latest)

    # -- drain (GUI thread) --------------------------------------------------
    def _drain(self) -> None:
        if not self._inbox:
            return
        batch: list[Reading] = []
        while True:
            try:
                batch.append(self._inbox.popleft())
            except IndexError:
                break
        for r in batch:
            self._latest[r.key] = r
        for sink in list(self._sinks):
            try:
                sink(batch)
            except Exception:
                pass
        self.tick.emit()

    def shutdown(self) -> None:
        self._timer.stop()
        for d in list(self._devices.values()):
            try:
                d.stop()
            except Exception:
                pass
