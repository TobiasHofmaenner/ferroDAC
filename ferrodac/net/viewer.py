"""HubViewer — consume a hub's devices + live readings.

Watches the catalog (remote devices) and subscribes to their readings, handing
both to callbacks. The Qt side turns catalog events into device ports (§6.1
'bind REMOTE') and feeds the readings into the Engine, so remote devices render
exactly like local ones. Runs grpc.aio in its own thread; callbacks fire on that
thread (marshal to the GUI thread on the Qt side).
"""

from __future__ import annotations

import asyncio
import logging

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import convert, watch_connectivity
from .session import ReconnectingClient

log = logging.getLogger("hub.viewer")


class HubViewer(ReconnectingClient):
    _thread_name = "hub-viewer"
    _disconnect_label = "hub"

    def __init__(self, addr: str, on_catalog=None, on_readings=None,
                 on_state=None):
        super().__init__(addr, on_state)       # thread / loop / stop / reconnect FSM
        self._on_catalog = on_catalog          # (event_type: str, pb.DeviceDescriptor)
        self._on_readings = on_readings        # (list[app Reading])

    async def _run_session(self, ch) -> None:
        v = rpc.ViewerStub(ch)
        # REAL link from the channel state (not 'we opened a channel'); watch the
        # catalog + subscribe to readings until any ends (disconnect) or we stop.
        conn = asyncio.create_task(watch_connectivity(ch, self._addr, self._notify))
        watch = asyncio.create_task(self._watch(v))
        sub = asyncio.create_task(self._subscribe(v))
        stopper = asyncio.create_task(self._stop.wait())
        await asyncio.wait({conn, watch, sub, stopper},
                           return_when=asyncio.FIRST_COMPLETED)
        for t in (conn, watch, sub, stopper):
            t.cancel()
        await asyncio.gather(conn, watch, sub, stopper, return_exceptions=True)

    async def _watch(self, v) -> None:
        async for ev in v.WatchCatalog(pb.CatalogRequest()):
            if self._on_catalog is not None:
                self._on_catalog(pb.CatalogEvent.Type.Name(ev.type), ev.device)

    async def _subscribe(self, v) -> None:
        async for batch in v.Subscribe(pb.SubscribeRequest()):
            if self._on_readings is not None and batch.readings:
                self._on_readings(
                    [convert.reading_from_proto(r) for r in batch.readings])
