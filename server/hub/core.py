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
        # Tags (DESIGN §7.3): a durable, reliable store keyed by id, merged
        # last-write-wins on version, tombstones kept so deletes propagate.
        # Separate from readings — never drop-oldest. In-memory in M1.
        self._tags: dict[str, pb.Tag] = {}
        self._tag_watchers: set[asyncio.Queue] = set()

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

    # -- tags (own reliable channel; LWW by id+version, tombstoned) ----------
    def tag_snapshot(self) -> list:
        """Every stored tag — live AND tombstones — so a reconnecting peer
        converges (it may need a delete it missed while away)."""
        return list(self._tags.values())

    def add_tag_watcher(self, q: "asyncio.Queue") -> None:
        self._tag_watchers.add(q)

    def remove_tag_watcher(self, q: "asyncio.Queue") -> None:
        self._tag_watchers.discard(q)

    def publish_tag(self, tag: pb.Tag) -> bool:
        """Merge a tag, last-write-wins on version. Returns True if it changed
        our state (and was fanned out), False if stale/duplicate."""
        cur = self._tags.get(tag.id)
        if cur is not None and tag.version < cur.version:
            return False                         # stale — older than what we have
        if cur is not None and tag.version == cur.version \
                and not tag.deleted and not cur.deleted:
            return False                         # idempotent same-version upsert
        self._tags[tag.id] = tag
        if tag.deleted:
            etype = pb.TagEvent.REMOVED
        elif cur is None:
            etype = pb.TagEvent.ADDED
        else:
            etype = pb.TagEvent.UPDATED
        self._emit_tag(pb.TagEvent(type=etype, tag=tag))
        return True

    def delete_tag(self, tag_id: str, version: int, origin_id: str = "") -> bool:
        """Tombstone a tag. The tombstone's version must beat the live one to
        win LWW; bump it if the caller's is too low. Carries the live tag's
        context (t/kind/label) into the REMOVED event for the audit log."""
        cur = self._tags.get(tag_id)
        if cur is not None and version <= cur.version:
            version = cur.version + 1
        tomb = pb.Tag(id=tag_id, version=version, deleted=True,
                      origin_id=origin_id)
        if cur is not None:
            tomb.t, tomb.kind, tomb.label = cur.t, cur.kind, cur.label
            tomb.scope, tomb.severity = cur.scope, cur.severity
        return self.publish_tag(tomb)

    def _emit_tag(self, event: pb.TagEvent) -> None:
        for q in self._tag_watchers:
            q.put_nowait(event)                  # unbounded queue — tags are reliable
