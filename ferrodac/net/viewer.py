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
import threading

import grpc
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import GRPC_CHANNEL_OPTIONS, _drain, convert

log = logging.getLogger("hub.viewer")


class HubViewer:
    def __init__(self, addr: str, on_catalog=None, on_readings=None,
                 on_state=None):
        self._addr = addr
        self._on_catalog = on_catalog          # (event_type: str, pb.DeviceDescriptor)
        self._on_readings = on_readings        # (list[app Reading])
        self._on_state = on_state
        self._thread: "threading.Thread | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._stop: "asyncio.Event | None" = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="hub-viewer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and self._stop is not None:
            loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _notify(self, connected: bool, detail: str) -> None:
        log.info("%s", detail)
        if self._on_state is not None:
            try:
                self._on_state(connected, detail)
            except Exception:
                pass

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._stop = asyncio.Event()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        finally:
            _drain(loop)
            loop.close()

    async def _main(self) -> None:
        while not self._stop.is_set():
            try:
                async with grpc.aio.insecure_channel(
                        self._addr, options=GRPC_CHANNEL_OPTIONS) as ch:
                    v = rpc.ViewerStub(ch)
                    self._notify(True, f"connected to {self._addr}")
                    watch = asyncio.create_task(self._watch(v))
                    sub = asyncio.create_task(self._subscribe(v))
                    stopper = asyncio.create_task(self._stop.wait())
                    await asyncio.wait({watch, sub, stopper},
                                       return_when=asyncio.FIRST_COMPLETED)
                    for t in (watch, sub, stopper):
                        t.cancel()
                    await asyncio.gather(watch, sub, stopper,
                                         return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._notify(False, f"hub disconnected: {e}")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def _watch(self, v) -> None:
        async for ev in v.WatchCatalog(pb.CatalogRequest()):
            if self._on_catalog is not None:
                self._on_catalog(pb.CatalogEvent.Type.Name(ev.type), ev.device)

    async def _subscribe(self, v) -> None:
        async for batch in v.Subscribe(pb.SubscribeRequest()):
            if self._on_readings is not None and batch.readings:
                self._on_readings(
                    [convert.reading_from_proto(r) for r in batch.readings])
