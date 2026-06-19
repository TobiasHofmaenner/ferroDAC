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


class TagsServicer(rpc.TagsServicer):
    """Tags — role-independent (any client may publish/delete/watch). Tags ride
    their own reliable channel, never the Reading stream (DESIGN §7.3)."""

    def __init__(self, hub: Hub):
        self.hub = hub

    async def PublishTag(self, request, context):  # noqa: N802
        changed = self.hub.publish_tag(request.tag)
        return pb.TagAck(ok=True, detail="" if changed else "stale/duplicate")

    async def DeleteTag(self, request, context):  # noqa: N802
        self.hub.delete_tag(request.id, request.version, request.origin_id)
        return pb.TagAck(ok=True)

    async def WatchTags(self, request, context):  # noqa: N802
        q: asyncio.Queue = asyncio.Queue()       # unbounded — tags are reliable
        # register BEFORE snapshotting (same as WatchCatalog): a tag caught in
        # both the snapshot and the live stream arrives twice, which the client
        # merges idempotently by id+version.
        self.hub.add_tag_watcher(q)
        try:
            for tag in self.hub.tag_snapshot():
                etype = pb.TagEvent.REMOVED if tag.deleted else pb.TagEvent.ADDED
                yield pb.TagEvent(type=etype, tag=tag)
            while True:
                yield await q.get()
        except asyncio.CancelledError:      # client/stream went away — clean end
            pass
        finally:
            self.hub.remove_tag_watcher(q)


# --------------------------------------------------------------------------- #
#  Store (DESIGN §7.4 / §12.1) — durable historic data, both directions:
#  sync (agent→hub: GetSyncState + PushChunk) and read (client→hub: as a
#  resolver tier). Thin wrappers over a ZarrStore (the same store the app runs).
# --------------------------------------------------------------------------- #
import numpy as np


def _chunk_from_pb(req) -> dict:
    if req.dtype == "trace":
        t = np.asarray(req.t, dtype="f8")
        m = int(req.m) or (len(req.y) // len(t) if len(t) else 0)
        y = np.asarray(req.y, dtype="f8").reshape(len(t), m) if m else np.zeros((0, 0))
        return {"dtype": "trace", "t": t, "y": y, "x": np.asarray(req.x, dtype="f8")}
    return {"dtype": "scalar", "t": np.asarray(req.t, dtype="f8"),
            "v": np.asarray(req.v, dtype="f8")}


class StoreServicer(rpc.StoreServicer):
    def __init__(self, store):
        self.store = store

    async def GetSyncState(self, request, context):  # noqa: N802
        if self.store is None:
            return pb.SyncState()
        epochs = [pb.EpochLen(source=s, epoch=e, n=n)
                  for (s, e), n in self.store.epoch_lengths().items()]
        return pb.SyncState(epochs=epochs)

    async def PushChunk(self, request, context):  # noqa: N802
        if self.store is None:
            return pb.ChunkAck(n=0)
        n = self.store.apply_chunk(request.source, request.epoch, _chunk_from_pb(request))
        return pb.ChunkAck(n=n)

    async def ListSources(self, request, context):  # noqa: N802
        if self.store is None:
            return pb.Sources()
        out = []
        for key in self.store.sources():
            name, unit, dtype = self.store.source_meta(key)
            out.append(pb.SourceInfo(key=key, name=name, unit=unit, dtype=dtype))
        return pb.Sources(sources=out)

    async def GetCoverage(self, request, context):  # noqa: N802
        ivs = []
        if self.store is not None:
            try:
                ivs = [pb.Interval(t0=a, t1=b)
                       for (a, b) in self.store.coverage(request.source)]
            except Exception:                       # noqa: BLE001  unknown source
                pass
        return pb.Coverage(intervals=ivs)

    async def Query(self, request, context):  # noqa: N802
        x = y = []
        if self.store is not None:
            try:
                qx, qy = self.store.query(request.source, request.t0, request.t1,
                                          request.max_points or 2000)
                x, y = [float(v) for v in qx], [float(v) for v in qy]
            except Exception:                       # noqa: BLE001
                pass
        return pb.Series(x=x, y=y)

    async def ReadRaw(self, request, context):  # noqa: N802
        t = v = []
        if self.store is not None:
            try:
                rt, rv = self.store.read_raw(request.source, request.t0, request.t1)
                t, v = [float(x) for x in rt], [float(x) for x in rv]
            except Exception:                       # noqa: BLE001
                pass
        return pb.RawScalar(t=t, v=v)

    async def ReadRawTrace(self, request, context):  # noqa: N802
        blocks = []
        if self.store is not None:
            try:
                for times, Y, x in self.store.read_raw_trace(
                        request.source, request.t0, request.t1):
                    Y = np.asarray(Y)
                    m = int(Y.shape[1]) if Y.ndim == 2 else 0
                    blocks.append(pb.TraceBlock(
                        t=[float(v) for v in times],
                        y=[float(v) for v in Y.reshape(-1)],
                        x=[float(v) for v in np.asarray(x)], m=m))
            except Exception:                       # noqa: BLE001
                pass
        return pb.RawTrace(blocks=blocks)
