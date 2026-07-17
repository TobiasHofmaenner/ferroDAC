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
    provenance_changed = Signal()   # a device's σ model (or other provenance) changed
    device_added = Signal(str)      # a device the USER just added (instance_id) — NOT a
    #                                 discovery/session re-add; the UI auto-curates its
    #                                 channels into the active project (never on restore)
    device_tag = Signal(object)     # a device raised a tag (Marker) — alarm / event /
    #                                 gas-detected (DESIGN §7.3). Fired from a device's
    #                                 poll thread; the app connects it with a QueuedConnection.
    device_prompt = Signal(object, object)  # a device raised an operator REQUEST — carries
    #                                 (Prompt, on_response). The request/response analogue of
    #                                 device_tag (core.interaction); same poll-thread → GUI
    #                                 QueuedConnection marshalling.
    device_removed = Signal(object)  # a device was REMOVED (user remove) — carries its
    #                                 (uuid, instance_id). The app withdraws that device's
    #                                 open prompts so none linger with a dead-driver callback.
    device_prompt_withdrawn = Signal(str)  # a device RESOLVED one of its own prompts (its
    #                                 front panel / another transport, by prompt id) — the app
    #                                 retires it from the inbox, no answer (core.interaction).

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
    def _wire_tags(self, device) -> None:
        """Give the device its platform-INJECTED device→app channels, so a driver needs
        zero per-device plumbing: the device→tag emitter (DESIGN §7.3) and the
        device→app→device request/response channel (core.interaction). The device fires
        both on its poll/reader thread; the app connects ``device_tag`` / ``device_prompt``
        with a QueuedConnection to marshal them onto the GUI thread."""
        if hasattr(device, "set_tag_sink"):
            device.set_tag_sink(self.device_tag.emit)
        if hasattr(device, "set_prompt_sink"):
            device.set_prompt_sink(self.device_prompt.emit)
        if hasattr(device, "set_prompt_withdraw_sink"):
            device.set_prompt_withdraw_sink(self.device_prompt_withdrawn.emit)

    def add(self, instance_id: str, *, user: bool = False) -> None:
        """Activate a device. ``user=True`` marks an explicit user add (the Devices
        panel) vs. an automatic discovery/session re-add (``_try_resolve``) — only the
        former emits ``device_added`` so the UI auto-curates channels without doing it
        on every restart/reconnect."""
        device = self._available.pop(instance_id, None)
        if device is None:
            return
        # Onboard: assign the device's stable UUID before it starts streaming, so
        # every Reading is keyed by the data-plane identity from the first sample.
        if hasattr(device, "fingerprint") and device.uuid is None:
            uid = self._registry.register(device.fingerprint, friendly=device.name)
            device.set_uuid(uid)
        self._active[instance_id] = device
        self._wire_tags(device)
        if hasattr(device, "mark_connecting"):
            device.mark_connecting()
        self.available_changed.emit()
        self.active_changed.emit()
        if user:
            self.device_added.emit(instance_id)

        def _connect_and_stream():
            device.connect()
            if self._engine is not None:
                self._engine.start_device(device)

        self._run_async(_connect_and_stream, on_finished=self.active_changed.emit)

    def add_user_device(self, device, *, user: bool = True) -> None:
        """Activate a device the APP minted itself (a Python device, or any
        discoverable=False driver) — one that never came from a discovery scan. Mirrors
        add() but injects the live object straight into _active instead of pulling it
        from _available: the discovery worker OWNS _available and purges any entry its
        scan didn't re-report, so a non-discovered device parked there would vanish on
        the next tick. It must go straight to _active. user=True (an explicit mint) emits
        device_added so the UI auto-curates its channels; pass user=False for a startup
        restore (re-minting saved defs), which must NOT auto-curate."""
        iid = device.instance_id
        if iid in self._active:
            return
        if hasattr(device, "fingerprint") and device.uuid is None:
            uid = self._registry.register(device.fingerprint, friendly=device.name)
            device.set_uuid(uid)
        self._active[iid] = device
        self._wire_tags(device)
        if hasattr(device, "mark_connecting"):
            device.mark_connecting()
        self.active_changed.emit()          # _available untouched → no available_changed
        if user:
            self.device_added.emit(iid)

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
        # Retire any operator REQUESTS this device left open, so they can't linger in the
        # inbox with a callback into a driver we're about to disconnect (core.interaction).
        self.device_removed.emit((getattr(device, "uuid", None), instance_id))
        # A user-minted device (Python device) persists its definition so it survives a
        # restart; an explicit user remove must drop that def too, else it'd be re-minted
        # on the next launch. on_forget() is the device's own cleanup hook — absent (a
        # no-op) on hardware drivers. remove() is ALWAYS user-initiated (never a transient
        # "device lost"), so forgetting the def here is safe.
        forget = getattr(device, "on_forget", None)
        if forget is not None:
            try:
                forget()
            except Exception:               # noqa: BLE001 — cleanup must not block removal
                log.exception("on_forget failed for %s", instance_id)

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

        def _finished():
            if not silent:
                self.active_changed.emit()
            # A σ-model re-declaration (e.g. Keithley range) rides even SILENT writes,
            # so its change-log entry is captured without a full active_changed rebuild.
            if device.take_provenance_dirty():
                self.provenance_changed.emit()

        self._run_async(lambda: device.write(sink_id, value), on_finished=_finished)

    def write_sync(self, instance_id: str, sink_id: str, value=None) -> "tuple[bool, str]":
        """Synchronous sink write returning (ok, detail) — the control plane's remote
        command path (§5.3): the agent runs this OFF its loop (so a blocking device
        write can't stall the session) and Acks the result. Emits active_changed /
        provenance so local views and the device's readback source update exactly like
        a UI write. Not for the GUI thread — device.write blocks on the I/O lock."""
        device = self._active.get(instance_id)
        if device is None:
            return False, "device not active"
        try:
            device.write(sink_id, value)
        except Exception as exc:                # noqa: BLE001 — the reason goes in the Ack
            return False, str(exc)
        self.active_changed.emit()
        if device.take_provenance_dirty():
            self.provenance_changed.emit()
        return True, ""

    # -- synchronous configure (control plane §5.3, remote SetConfig) ---------
    # The agent runs these OFF its loop and Acks (ok, detail). They emit the same
    # signals as the local config dialog, so the re-announced descriptor carries the
    # new state back to the viewer (the config readback).
    def set_option_sync(self, instance_id: str, key: str, value) -> "tuple[bool, str]":
        device = self._active.get(instance_id) or self._available.get(instance_id)
        if device is None or not hasattr(device, "set_option"):
            return False, "device not found"
        try:
            device.set_option(key, value)        # inline (agent is already off-loop)
        except Exception as exc:                 # noqa: BLE001 — reason → Ack
            return False, str(exc)
        self.active_changed.emit()
        self.available_changed.emit()
        return True, ""

    def set_rate_sync(self, instance_id: str, hz: float) -> "tuple[bool, str]":
        device = self._active.get(instance_id)
        if device is None or not hasattr(device, "set_rate_hz"):
            return False, "device not active or fixed-rate"
        device.set_rate_hz(float(hz))
        self.active_changed.emit()
        return True, ""

    def rename_sync(self, instance_id: str, name: str) -> "tuple[bool, str]":
        device = self._active.get(instance_id) or self._available.get(instance_id)
        if device is None or not hasattr(device, "set_name"):
            return False, "device not found"
        device.set_name(name)
        self.active_changed.emit()
        self.available_changed.emit()
        return True, ""

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

        def _changed():
            self.active_changed.emit()
            self.available_changed.emit()

        if getattr(device, "async_config", False):
            # set_option may block (cloud enumeration) — apply it off the GUI thread,
            # then refresh once the worker reports back on the GUI thread.
            self._run_async(lambda: device.set_option(key, value), on_finished=_changed)
        else:
            device.set_option(key, value)
            _changed()

    def check(self, instance_id: str, on_result) -> None:
        """Run a device's connection check OFF the GUI thread; call ``on_result``
        with the CheckResult on the GUI thread when done (drives the config GUI's
        "Check connection" button)."""
        from .device import CheckResult
        device = self._active.get(instance_id) or self._available.get(instance_id)
        if device is None:
            on_result(CheckResult(False, "Device not found."))
            return
        box = {}
        self._run_async(
            lambda: box.__setitem__("r", device.check()),
            on_finished=lambda: on_result(box.get("r")
                                          or CheckResult(False, "Check failed.")),
        )

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

        def apply():
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

        # An async_config device's set_option can BLOCK (cloud enumeration — Shelly
        # sleeps ~1 s per request): never on the GUI thread. The explicit-config path
        # already offloads it; this session-restore / discovery resolve path did NOT,
        # so a Shelly with bad creds froze the UI ~2 s on every discovery tick (watchdog).
        if getattr(device, "async_config", False):
            self._run_async(apply, on_finished=self.active_changed.emit)
        else:
            apply()
            self.active_changed.emit()

    def descriptor(self, instance_id: str) -> DeviceDescriptor | None:
        device = self._active.get(instance_id) or self._available.get(instance_id)
        return device.describe() if device else None

    def available_descriptors(self) -> list[DeviceDescriptor]:
        return [d.describe() for d in self._available.values()]

    def active_descriptors(self) -> list[DeviceDescriptor]:
        return [d.describe() for d in self._active.values()]

    def active_devices(self) -> list:
        """The live Device OBJECTS (not descriptors) — for capability-probed
        cross-cutting services (e.g. the media plane's clip recorder asking
        cameras to start/stop, DESIGN §9). Read-only snapshot."""
        return list(self._active.values())

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
