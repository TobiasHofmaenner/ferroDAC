"""DeviceManager — background discovery + available/active device state."""

from __future__ import annotations

import logging
from typing import Callable, Sequence

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QObject, QThread, Signal

from .device import Device, DeviceDescriptor
from .identity import DeviceRegistry, Fingerprint

log = logging.getLogger("manager")


class _DiscoveryWorker(QThread):
    found = Signal(list)  # list[Device]

    def __init__(self, drivers: Sequence[type[Device]], interval: float, parent=None):
        super().__init__(parent)
        self._drivers = list(drivers)
        self._interval = interval
        self._running = False

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        self._running = True
        while self._running:
            found: list[Device] = []
            for drv in self._drivers:
                try:
                    found.extend(drv.discover())
                except Exception:
                    pass
            if self._running:
                self.found.emit(found)
            slept = 0.0
            while self._running and slept < self._interval:
                self.msleep(100)
                slept += 0.1


class _OpWorker(QThread):
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


class DeviceManager(QObject):
    available_changed = Signal()
    active_changed = Signal()

    def __init__(
        self,
        drivers: Sequence[type[Device]],
        scan_interval: float = 2.0,
        engine=None,
        registry: DeviceRegistry | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._discoverable = [d for d in drivers if getattr(d, "discoverable", False)]
        self._available: dict[str, Device] = {}
        self._active: dict[str, Device] = {}
        self._workers: list[_OpWorker] = []
        self._engine = engine
        self._registry = registry if registry is not None else DeviceRegistry()
        self._pending: dict[str, dict] = {}     # uuid -> desired config (session restore)
        self._resolving = False

        self._scan = _DiscoveryWorker(self._discoverable, scan_interval)
        self._scan.found.connect(self._merge_found)
        self.available_changed.connect(self._try_resolve)
        self.active_changed.connect(self._try_resolve)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._discoverable and not self._scan.isRunning():
            # GUI-thread setup BEFORE the worker scans: a driver may need to touch
            # thread-affine Qt subsystems (e.g. the camera brings up Qt Multimedia
            # here, not on the discovery worker — see CameraDevice.prepare_discovery).
            for drv in self._discoverable:
                prep = getattr(drv, "prepare_discovery", None)
                if prep is not None:
                    try:
                        prep()
                    except Exception:            # noqa: BLE001
                        log.exception("prepare_discovery failed: %s", drv)
            self._scan.start()

    def stop(self) -> None:
        self._scan.stop()
        self._scan.wait(3000)
        for d in list(self._active.values()):
            try:
                if self._engine is not None:
                    self._engine.stop_device(d)
                d.disconnect()
            except Exception:
                pass
        for w in list(self._workers):
            w.wait(2000)

    # -- discovery merge -----------------------------------------------------
    def _merge_found(self, found: list) -> None:
        log.info("discovery found %d device(s): %s", len(found),
                 ", ".join(d.instance_id for d in found) or "—")
        seen = {d.instance_id for d in found}
        changed = False
        for d in found:
            iid = d.instance_id
            if iid in self._active or iid in self._available:
                continue
            self._available[iid] = d
            changed = True
        for iid in list(self._available):
            if iid not in seen:
                del self._available[iid]
                changed = True
        if changed:
            self.available_changed.emit()

    # -- user actions --------------------------------------------------------
    def add(self, instance_id: str) -> None:
        device = self._available.pop(instance_id, None)
        if device is None:
            return
        # Onboard: assign the device's stable UUID before it starts streaming, so
        # every Reading is keyed by the data-plane identity from the first sample.
        if hasattr(device, "fingerprint") and device.uuid is None:
            uid = self._registry.register(device.fingerprint, friendly=device.name)
            device.set_uuid(uid)
        self._active[instance_id] = device
        if hasattr(device, "mark_connecting"):
            device.mark_connecting()
        self.available_changed.emit()
        self.active_changed.emit()

        def _connect_and_stream():
            device.connect()
            if self._engine is not None:
                self._engine.start_device(device)

        self._run_async(_connect_and_stream, on_finished=self.active_changed.emit)

    def remove(self, instance_id: str) -> None:
        device = self._active.pop(instance_id, None)
        if device is None:
            return
        self.active_changed.emit()

        def _stop_and_disconnect():
            if self._engine is not None:
                self._engine.stop_device(device)
            device.disconnect()

        self._run_async(_stop_and_disconnect)

    # -- sinks (control) -----------------------------------------------------
    def write(self, instance_id: str, sink_id: str, value=None, silent: bool = False) -> None:
        """Write to a device sink (off-thread). `silent` skips the active_changed
        refresh — used for high-rate routed writes (the UI polls values on tick)."""
        device = self._active.get(instance_id)
        if device is None:
            return
        self._run_async(
            lambda: device.write(sink_id, value),
            on_finished=None if silent else self.active_changed.emit,
        )

    def set_rate(self, instance_id: str, hz: float) -> None:
        device = self._active.get(instance_id)
        if device is None or not hasattr(device, "set_rate_hz"):
            return
        device.set_rate_hz(hz)
        self.active_changed.emit()

    def set_option(self, instance_id: str, key: str, value) -> None:
        device = self._active.get(instance_id) or self._available.get(instance_id)
        if device is None or not hasattr(device, "set_option"):
            return
        device.set_option(key, value)
        self.active_changed.emit()
        self.available_changed.emit()

    def rename(self, instance_id: str, name: str) -> None:
        device = self._active.get(instance_id) or self._available.get(instance_id)
        if device is None or not hasattr(device, "set_name"):
            return
        device.set_name(name)
        self.active_changed.emit()
        self.available_changed.emit()

    def is_active(self, instance_id: str) -> bool:
        return instance_id in self._active

    # -- resolution (uuid <-> live device) -----------------------------------
    @property
    def registry(self) -> DeviceRegistry:
        return self._registry

    def instance_for_uuid(self, uuid: str) -> str | None:
        """The instance_id of the active device carrying this UUID, if any."""
        for iid, dev in self._active.items():
            if getattr(dev, "uuid", None) == uuid:
                return iid
        return None

    def available_for_uuid(self, uuid: str) -> str | None:
        """An *available* (not yet active) device whose fingerprint matches the
        registry's fingerprint for this UUID — the resolver's local branch."""
        fp = self._registry.fingerprint_for(uuid)
        if fp is None:
            return None
        for iid, dev in self._available.items():
            if getattr(dev, "fingerprint", None) == fp:
                return iid
        return None

    # -- session restore -----------------------------------------------------
    def export_active(self) -> list[dict]:
        """Serialize active devices (uuid + fingerprint + config) for a session."""
        out = []
        for dev in self._active.values():
            d = dev.describe()
            fp = dev.fingerprint
            out.append({
                "uuid": d.uuid, "driver": fp.driver, "hardware_id": fp.hardware_id,
                "friendly": d.name,
                "options": {o.key: o.value for o in d.options},
                "rate_hz": d.rate_hz,
                "sink_values": {s.id: s.value for s in d.sinks if s.value is not None},
            })
        return out

    def request_devices(self, entries: list[dict]) -> None:
        """Make these devices (by uuid+fingerprint) active as they appear, then
        apply their saved config. The resolver's local branch; the hub branch
        (Phase 2) plugs in here too."""
        for e in entries:
            uuid = e.get("uuid")
            if not uuid:
                continue
            self._registry.adopt(uuid, Fingerprint(e["driver"], e["hardware_id"]),
                                 e.get("friendly", ""))
            self._pending[uuid] = e
        self._try_resolve()

    def _try_resolve(self) -> None:
        if self._resolving or not self._pending:
            return
        self._resolving = True
        try:
            for uuid, entry in list(self._pending.items()):
                inst = self.instance_for_uuid(uuid)
                if inst is not None:
                    self._apply_device_config(inst, entry)
                    self._pending.pop(uuid, None)
                else:
                    avail = self.available_for_uuid(uuid)
                    if avail is not None:
                        self.add(avail)     # config applied on a later resolve pass
        finally:
            self._resolving = False

    def _apply_device_config(self, instance_id: str, entry: dict) -> None:
        device = self._active.get(instance_id)
        if device is None:
            return
        for key, value in entry.get("options", {}).items():
            if hasattr(device, "set_option"):
                device.set_option(key, value)
        hz = entry.get("rate_hz")
        if hz and hasattr(device, "set_rate_hz"):
            device.set_rate_hz(hz)
        for sid, value in entry.get("sink_values", {}).items():
            try:
                device.write(sid, value)
            except Exception:
                pass
        self.active_changed.emit()

    def descriptor(self, instance_id: str) -> DeviceDescriptor | None:
        device = self._active.get(instance_id) or self._available.get(instance_id)
        return device.describe() if device else None

    def available_descriptors(self) -> list[DeviceDescriptor]:
        return [d.describe() for d in self._available.values()]

    def active_descriptors(self) -> list[DeviceDescriptor]:
        return [d.describe() for d in self._active.values()]

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
