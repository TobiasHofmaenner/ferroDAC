"""Bus — the Qt-free data-plane core (DESIGN §4.1; execution model §21).

The publish/subscribe/batch-drain mechanics, with no Qt: producers `publish`
readings (thread-safe append), consumers `subscribe` as sinks, and a caller
`drain`s the buffer into one batch and fans it out. The live `Engine` (Qt) wraps
a Bus and drives `drain()` from a `QTimer`; the **replay** context (DESIGN §7.4)
drives its own Bus on its own loop — same mechanics, no event loop required. So
"who pumps the bus" is the only difference between live and headless.

Thread affinity (DESIGN §21.2) is declared AT SUBSCRIPTION:

- ``thread="inline"`` (default): the sink runs during ``drain()`` on whatever
  thread pumps the bus — the GUI thread in the live app. The only lane that may
  touch Qt.
- ``thread="worker"``: the Bus owns ONE plain thread per worker sink
  (``fd-bus-<name>``), so a blocked or slow sink (e.g. a third-party plugin) can
  only stall itself — never the store writer. ``drain()`` just enqueues the
  batch per lane and never waits on a worker. Worker sinks MUST NOT touch Qt
  (QObjects are finalized on the GUI thread — DESIGN §21.1 I-1).
- ``mode="lossless"``: unbounded FIFO, never drops, fully delivered on close —
  the durable writer's guarantee. ``mode="conflate"``: backlog beyond a few
  batches is merged in order and, past a reading cap, oldest-dropped (counted) —
  for sinks where only freshness matters (hub live feed, processors).

Ordering: per sink, batches arrive in drain order and publish order within a
batch — so per-source order holds everywhere. No ordering is promised BETWEEN
sinks (the writer may lag the chart); that relaxation is what buys isolation.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

from .reading import Reading

log = logging.getLogger(__name__)

Sink = Callable[[list], None]            # Callable[[list[Reading]], None]

INLINE = "inline"
WORKER = "worker"
LOSSLESS = "lossless"
CONFLATE = "conflate"

_CONFLATE_DEPTH = 8          # pending batches before a conflate pump merges them
_HIGH_WATER = 100            # lossless backlog (batches) that logs a warning


class _Pump:
    """One plain worker thread servicing one sink's batch FIFO (Qt-free)."""

    def __init__(self, sink: Sink, mode: str, name: str, conflate_max: int):
        self._sink = sink
        self._mode = mode
        self.name = name
        self._conflate_max = conflate_max
        self._q: deque = deque()          # of batches (lists of Readings)
        self._cv = threading.Condition()
        self._stopping = False
        self.delivered = 0                # readings handed to the sink
        self.dropped = 0                  # readings conflated away (mode=conflate)
        self.errors = 0                   # sink exceptions (isolated, counted)
        self._warned = False              # high-water warning latch
        self._thread = threading.Thread(target=self._run,
                                        name=f"fd-bus-{name}", daemon=True)
        self._thread.start()

    def put(self, batch: list) -> None:
        with self._cv:
            if self._stopping:
                return
            self._q.append(batch)
            if self._mode == CONFLATE and len(self._q) > _CONFLATE_DEPTH:
                merged: list = []
                while self._q:
                    merged.extend(self._q.popleft())
                if len(merged) > self._conflate_max:
                    self.dropped += len(merged) - self._conflate_max
                    merged = merged[-self._conflate_max:]
                self._q.append(merged)
            elif self._mode == LOSSLESS and len(self._q) > _HIGH_WATER:
                if not self._warned:
                    self._warned = True
                    log.warning("bus sink %r backlog: %d batches queued — the "
                                "consumer cannot keep up (data is safe in RAM)",
                                self.name, len(self._q))
            elif self._warned and len(self._q) < _HIGH_WATER // 2:
                self._warned = False
                log.info("bus sink %r backlog cleared", self.name)
            self._cv.notify()

    def depth(self) -> int:
        with self._cv:
            return len(self._q)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._stopping:
                    self._cv.wait()
                if self._q:
                    batch = self._q.popleft()
                else:                     # stopping and fully drained
                    return
            try:
                self._sink(batch)
                self.delivered += len(batch)
            except Exception:             # noqa: BLE001 — a sink must never kill the pump
                self.errors += 1
                if self.errors <= 3 or self.errors % 100 == 0:
                    log.exception("bus sink %r failed (x%d)", self.name, self.errors)

    def close(self, timeout: float = 5.0, flush: bool = True) -> bool:
        """Stop the pump. ``flush=True`` delivers everything still queued first
        (the lossless guarantee); ``flush=False`` discards the backlog. Returns
        True if the thread finished within `timeout` (it is a daemon either way,
        so process exit is never held hostage by a wedged sink)."""
        with self._cv:
            if not flush:
                self._q.clear()
            self._stopping = True
            self._cv.notify()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def stats(self) -> dict:
        return {"mode": self._mode, "depth": self.depth(),
                "delivered": self.delivered, "dropped": self.dropped,
                "errors": self.errors}


class Subscription:
    """Handle returned by ``subscribe()``. CALLABLE for back-compat: calling it
    unsubscribes (as the old closure did), flushing a lossless pump first."""

    def __init__(self, bus: "Bus", sink: Sink, thread: str, mode: str,
                 name: str, pump: "_Pump | None"):
        self._bus = bus
        self.sink = sink
        self.thread = thread
        self.mode = mode
        self.name = name
        self.pump = pump

    def __call__(self) -> None:
        self.close()

    def close(self, timeout: float = 5.0) -> bool:
        """Unsubscribe. For a worker-lane sink this delivers the queued backlog
        (lossless) or discards it (conflate), then joins the pump — so a caller
        like ``StoreWriter.stop()`` is self-sufficient regardless of shutdown
        order. Idempotent."""
        self._bus._remove(self)
        if self.pump is not None:
            return self.pump.close(timeout, flush=(self.mode == LOSSLESS))
        return True

    def stats(self) -> dict:
        base = {"thread": self.thread, "mode": self.mode}
        return {**base, **(self.pump.stats() if self.pump else {})}


class Bus:
    def __init__(self, on_pending: "Callable[[], None] | None" = None):
        self._inbox: deque = deque()     # thread-safe append / popleft
        self._latest: dict[str, Reading] = {}
        self._subs: list[Subscription] = []
        self._slock = threading.Lock()   # subscribe/unsubscribe vs drain snapshot
        # on_pending: fired (from the publishing thread, at most once until the
        # next drain re-arms it) when a publish lands — lets an owner schedule an
        # out-of-cadence pump for a bus nobody is ticking (parked replay bus with
        # worker-lane processors). Benign race on the latch: a missed arm only
        # delays delivery to the next natural pump.
        self._on_pending = on_pending
        self._pending_armed = on_pending is not None

    def publish(self, reading: Reading) -> None:
        """Push a reading in — cheap & thread-safe (called from acq threads)."""
        self._inbox.append(reading)
        if self._pending_armed:
            self._pending_armed = False
            try:
                self._on_pending()
            except Exception:             # noqa: BLE001 — observer must never break publish
                pass

    def subscribe(self, sink: Sink, thread: str = INLINE, mode: str = LOSSLESS,
                  name: str = "", conflate_max: int = 100_000) -> Subscription:
        """Register a sink (called with a batch of Readings). Affinity contract
        in the module docstring / DESIGN §21.2. Returns a callable Subscription
        (calling it unsubscribes — back-compat with the old closure)."""
        name = name or getattr(sink, "__qualname__", None) or repr(sink)
        pump = (_Pump(sink, mode, name, conflate_max)
                if thread == WORKER else None)
        sub = Subscription(self, sink, thread, mode, name, pump)
        with self._slock:
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._slock:
            if sub in self._subs:
                self._subs.remove(sub)

    def latest(self) -> dict:
        return dict(self._latest)

    def drain(self, max_batch: "int | None" = None) -> list:
        """Pop up to `max_batch` readings into one batch, update `latest`, run
        inline sinks with it, enqueue it to every worker pump (never waiting on
        them), and return the batch. The cap bounds a post-stall catch-up batch
        (DESIGN §21.2) — the remainder stays queued for the next drain: delayed,
        never dropped."""
        if not self._inbox:
            return []
        batch: list = []
        while max_batch is None or len(batch) < max_batch:
            try:
                batch.append(self._inbox.popleft())
            except IndexError:
                break
        if not batch:
            return []
        for r in batch:
            self._latest[r.key] = r
        with self._slock:
            subs = list(self._subs)
        for sub in subs:
            if sub.pump is not None:
                sub.pump.put(batch)
            else:
                try:
                    sub.sink(batch)
                except Exception:         # noqa: BLE001 — sinks are isolated
                    pass
        if self._on_pending is not None:
            self._pending_armed = True
        return batch

    def stats(self) -> dict:
        with self._slock:
            subs = list(self._subs)
        return {"inbox": len(self._inbox),
                "sinks": {s.name: s.stats() for s in subs}}

    def close(self, timeout: float = 5.0) -> None:
        """Close every worker pump (lossless backlogs are delivered first)."""
        with self._slock:
            subs = list(self._subs)
        for sub in subs:
            if sub.pump is not None:
                sub.close(timeout)
