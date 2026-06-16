"""Hub core: the in-memory catalog and the live fan-out.

No storage in Milestone 1 — the hub holds which devices are *currently* publishing
(announced by an agent's Session) and fans each ReadingBatch out to the viewers
subscribed to it. Devices vanish when their agent's session ends (→ placeholder
on viewers, §6.1). Everything here is pure asyncio, single event loop.
"""

from __future__ import annotations

import asyncio

from ferrodac_contract.v1 import data_plane_pb2 as pb

CONTRACT_VERSION = 1
HUB_VERSION = "0.1.0"


def _offer(q: "asyncio.Queue", item) -> None:
    """Non-blocking enqueue; drop the oldest on overflow. The live tier is
    expendable by design — a slow viewer must never block ingest or another
    viewer. (Durability is the recorded-bundle path, not this one.)"""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass


class Subscriber:
    """A live viewer stream. `refs` is the set of (device_uuid, source_id) it
    wants, or None for 'everything'."""

    __slots__ = ("queue", "refs")

    def __init__(self, refs: "set[tuple[str, str]] | None"):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.refs = refs

    def wants(self, device_uuid: str, source_id: str) -> bool:
        return self.refs is None or (device_uuid, source_id) in self.refs


class Hub:
    def __init__(self) -> None:
        self._devices: dict[str, pb.DeviceDescriptor] = {}
        self._subs: set[Subscriber] = set()
        self._watchers: set[asyncio.Queue] = set()

    # -- catalog -------------------------------------------------------------
    def snapshot(self) -> list:
        return list(self._devices.values())

    def announce(self, desc: pb.DeviceDescriptor) -> None:
        desc.online = True
        is_update = desc.uuid in self._devices
        self._devices[desc.uuid] = desc
        etype = pb.CatalogEvent.UPDATED if is_update else pb.CatalogEvent.ADDED
        self._emit_catalog(pb.CatalogEvent(type=etype, device=desc))

    def retire(self, device_uuid: str) -> None:
        desc = self._devices.pop(device_uuid, None)
        if desc is not None:
            self._emit_catalog(
                pb.CatalogEvent(type=pb.CatalogEvent.REMOVED, device=desc))

    def _emit_catalog(self, event: pb.CatalogEvent) -> None:
        for q in self._watchers:
            _offer(q, event)

    # -- watchers (WatchCatalog streams) ------------------------------------
    def add_watcher(self, q: "asyncio.Queue") -> None:
        self._watchers.add(q)

    def remove_watcher(self, q: "asyncio.Queue") -> None:
        self._watchers.discard(q)

    # -- subscribers (Subscribe streams) ------------------------------------
    def add_subscriber(self, sub: Subscriber) -> None:
        self._subs.add(sub)

    def remove_subscriber(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def publish(self, batch: pb.ReadingBatch) -> None:
        """Fan one ingest batch out to every interested subscriber."""
        if not self._subs:
            return
        for sub in self._subs:
            if sub.refs is None:
                _offer(sub.queue, batch)
            else:
                wanted = [r for r in batch.readings
                          if sub.wants(r.device_uuid, r.source_id)]
                if wanted:
                    _offer(sub.queue, pb.ReadingBatch(readings=wanted))
