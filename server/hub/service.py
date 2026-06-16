"""gRPC servicers: the two roles from the contract.

Ingest (agent → hub): one bidirectional Session per agent.
Viewer (client → hub): catalog snapshot/watch + live subscribe.

Auth is the reserved seam: the `token` on Hello/requests is accepted
unconditionally here (allow-all). Enforcement later becomes a metadata
interceptor — no servicer change.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from .core import CONTRACT_VERSION, HUB_VERSION, Hub, Subscriber

log = logging.getLogger("hub")


class IngestServicer(rpc.IngestServicer):
    """Agents dial out and hold one Session open. Up: hello/announce/readings/
    retire/heartbeat. Down: welcome now, commands later (reserved)."""

    def __init__(self, hub: Hub):
        self.hub = hub

    async def Session(self, request_iterator, context):  # noqa: N802
        mine: set[str] = set()          # devices this session announced
        agent = "?"
        try:
            async for msg in request_iterator:
                which = msg.WhichOneof("msg")
                if which == "hello":
                    agent = msg.hello.agent_id or "?"
                    log.info("agent connected: %s", agent)
                    yield pb.HubMessage(welcome=pb.Welcome(
                        session_id=_uuid.uuid4().hex,
                        contract_version=CONTRACT_VERSION,
                        hub_version=HUB_VERSION))
                elif which == "announce":
                    self.hub.announce(msg.announce)
                    mine.add(msg.announce.uuid)
                    log.info("announce: %s (%s) from %s",
                             msg.announce.name, msg.announce.uuid, agent)
                elif which == "readings":
                    self.hub.publish(msg.readings)
                elif which == "retire":
                    self.hub.retire(msg.retire.device_uuid)
                    mine.discard(msg.retire.device_uuid)
                elif which == "heartbeat":
                    pass
        finally:
            for device_uuid in mine:    # session ended ⇒ its devices vanish
                self.hub.retire(device_uuid)
            log.info("agent disconnected: %s (retired %d device(s))",
                     agent, len(mine))


class ViewerServicer(rpc.ViewerServicer):
    def __init__(self, hub: Hub):
        self.hub = hub

    async def GetInfo(self, request, context):  # noqa: N802
        return pb.HubInfo(hub_version=HUB_VERSION,
                          contract_version=CONTRACT_VERSION)

    async def GetCatalog(self, request, context):  # noqa: N802
        return pb.Catalog(devices=self.hub.snapshot())

    async def WatchCatalog(self, request, context):  # noqa: N802
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        # register BEFORE snapshotting so nothing is missed in the gap; a device
        # caught in both shows up as a duplicate ADDED, which viewers treat as an
        # idempotent upsert by uuid.
        self.hub.add_watcher(q)
        try:
            for desc in self.hub.snapshot():
                yield pb.CatalogEvent(type=pb.CatalogEvent.ADDED, device=desc)
            while True:
                yield await q.get()
        except asyncio.CancelledError:      # client/stream went away — clean end
            pass
        finally:
            self.hub.remove_watcher(q)

    async def Subscribe(self, request, context):  # noqa: N802
        refs = None
        if request.sources:
            refs = {(s.device_uuid, s.source_id) for s in request.sources}
        sub = Subscriber(refs)
        self.hub.add_subscriber(sub)
        try:
            while True:
                yield await sub.queue.get()
        except asyncio.CancelledError:      # client/stream went away — clean end
            pass
        finally:
            self.hub.remove_subscriber(sub)
