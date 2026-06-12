"""SourceManager — background discovery + available/active source state.

Responsibilities (v1):
  - periodically scan all discoverable driver types on a worker thread;
  - maintain `available` (discovered, not active) and `active` (connected)
    sets, **deduped by `instance_id`**;
  - let the UI add (connect) / remove (disconnect) sources without blocking.

The manager exposes everything to the UI as **descriptors**, never as Source
objects.
"""

from __future__ import annotations

from typing import Callable, Sequence

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QObject, QThread, Signal

from .source import Source, SourceDescriptor, Status


class _DiscoveryWorker(QThread):
    """Periodically calls `discover()` on every discoverable driver."""

    found = Signal(list)  # list[Source]

    def __init__(self, drivers: Sequence[type[Source]], interval: float, parent=None):
        super().__init__(parent)
        self._drivers = list(drivers)
        self._interval = interval
        self._running = False

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        self._running = True
        while self._running:
            found: list[Source] = []
            for drv in self._drivers:
                try:
                    found.extend(drv.discover())
                except Exception:
                    pass  # a flaky driver must not kill the scan
            if self._running:
                self.found.emit(found)
            slept = 0.0
            while self._running and slept < self._interval:
                self.msleep(100)
                slept += 0.1


class _OpWorker(QThread):
    """Runs one blocking source operation (connect/disconnect) off the UI thread."""

    done = Signal()
    failed = Signal(str)

    def __init__(self, fn: Callable[[], None], parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self._fn()
            self.done.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class SourceManager(QObject):
    available_changed = Signal()
    active_changed = Signal()

    def __init__(
        self,
        drivers: Sequence[type[Source]],
        scan_interval: float = 2.0,
        parent=None,
    ):
        super().__init__(parent)
        self._discoverable = [d for d in drivers if getattr(d, "discoverable", False)]
        self._available: dict[str, Source] = {}
        self._active: dict[str, Source] = {}
        self._workers: list[_OpWorker] = []

        self._scan = _DiscoveryWorker(self._discoverable, scan_interval)
        self._scan.found.connect(self._merge_found)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._discoverable and not self._scan.isRunning():
            self._scan.start()

    def stop(self) -> None:
        self._scan.stop()
        self._scan.wait(3000)
        for s in list(self._active.values()):
            try:
                s.disconnect()
            except Exception:
                pass

    # -- discovery merge -----------------------------------------------------
    def _merge_found(self, found: list) -> None:
        seen = {s.instance_id for s in found}
        changed = False

        for s in found:
            iid = s.instance_id
            if iid in self._active or iid in self._available:
                continue  # keep the existing object (and its state)
            self._available[iid] = s
            changed = True

        # Drop available sources that vanished (and aren't active).
        for iid in list(self._available):
            if iid not in seen:
                del self._available[iid]
                changed = True

        if changed:
            self.available_changed.emit()

    # -- user actions --------------------------------------------------------
    def add(self, instance_id: str) -> None:
        """Promote a discovered source to active and connect it."""
        source = self._available.pop(instance_id, None)
        if source is None:
            return
        self._active[instance_id] = source
        if hasattr(source, "mark_connecting"):
            source.mark_connecting()
        self.available_changed.emit()
        self.active_changed.emit()
        self._run_async(source.connect, on_finished=self.active_changed.emit)

    def remove(self, instance_id: str) -> None:
        """Disconnect an active source; it will reappear on the next scan."""
        source = self._active.pop(instance_id, None)
        if source is None:
            return
        self.active_changed.emit()
        self._run_async(source.disconnect)

    # -- configuration / controls -------------------------------------------
    def invoke(self, instance_id: str, control_id: str, value=None) -> None:
        """Invoke a control on an active source (off-thread; may hit hardware)."""
        source = self._active.get(instance_id)
        if source is None:
            return
        self._run_async(
            lambda: source.invoke(control_id, value),
            on_finished=self.active_changed.emit,
        )

    def set_rate(self, instance_id: str, hz: float) -> None:
        source = self._active.get(instance_id)
        if source is None or not hasattr(source, "set_rate_hz"):
            return
        source.set_rate_hz(hz)
        self.active_changed.emit()

    def rename(self, instance_id: str, name: str) -> None:
        source = self._active.get(instance_id) or self._available.get(instance_id)
        if source is None or not hasattr(source, "set_name"):
            return
        source.set_name(name)
        self.active_changed.emit()
        self.available_changed.emit()

    def is_active(self, instance_id: str) -> bool:
        return instance_id in self._active

    def descriptor(self, instance_id: str) -> SourceDescriptor | None:
        source = self._active.get(instance_id) or self._available.get(instance_id)
        return source.describe() if source else None

    # -- descriptors for the UI ---------------------------------------------
    def available_descriptors(self) -> list[SourceDescriptor]:
        return [s.describe() for s in self._available.values()]

    def active_descriptors(self) -> list[SourceDescriptor]:
        return [s.describe() for s in self._active.values()]

    # -- helpers -------------------------------------------------------------
    def _run_async(self, fn: Callable[[], None], on_finished=None) -> None:
        worker = _OpWorker(fn)

        def _cleanup(*_):
            if on_finished is not None:
                on_finished()
            if worker in self._workers:
                self._workers.remove(worker)

        worker.done.connect(_cleanup)
        worker.failed.connect(_cleanup)
        self._workers.append(worker)
        worker.start()
