"""Bus — the Qt-free data-plane core (DESIGN §4.1).

The publish/subscribe/batch-drain mechanics, with no Qt: producers `publish`
readings (thread-safe append), consumers `subscribe` as sinks, and a caller
`drain`s the buffer into one batch and fans it out. The live `Engine` (Qt) wraps
a Bus and drives `drain()` from a `QTimer`; the **replay** context (DESIGN §7.4)
drives its own Bus on its own loop — same mechanics, no event loop required. So
"who pumps the bus" is the only difference between live and headless.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

from .reading import Reading

Sink = Callable[[list], None]            # Callable[[list[Reading]], None]


class Bus:
    def __init__(self):
        self._inbox: deque = deque()     # thread-safe append / popleft
        self._latest: dict[str, Reading] = {}
        self._sinks: list[Sink] = []

    def publish(self, reading: Reading) -> None:
        """Push a reading in — cheap & thread-safe (called from acq threads)."""
        self._inbox.append(reading)

    def subscribe(self, sink: Sink):
        """Register a sink (called with a batch of Readings on drain). Returns
        an unsubscribe callable."""
        self._sinks.append(sink)

        def _unsub():
            if sink in self._sinks:
                self._sinks.remove(sink)

        return _unsub

    def latest(self) -> dict:
        return dict(self._latest)

    def drain(self) -> list:
        """Pop the whole buffer into one batch, update `latest`, fan out to every
        sink, and return the batch (empty if nothing was queued)."""
        if not self._inbox:
            return []
        batch: list = []
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
        return batch
