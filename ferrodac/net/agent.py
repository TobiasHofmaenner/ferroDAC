"""HubAgent — publish local devices + their readings to a hub.

Runs grpc.aio in its own thread (the app owns the Qt loop); the public methods
are called from the GUI thread and marshal onto the agent loop. Reconnects with
backoff and re-announces its devices on every (re)connect, so the hub's view
self-heals. The agent dials *out* — egress only.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import grpc
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import CONTRACT_VERSION, _drain, convert

log = logging.getLogger("hub.agent")


class HubAgent:
    def __init__(self, addr: str, agent_id: str = "ferrodac", on_state=None):
        self._addr = addr
        self._agent_id = agent_id
        self._on_state = on_state              # callback(connected: bool, detail: str)
        self._thread: "threading.Thread | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._outq: "asyncio.Queue | None" = None
        self._stop: "asyncio.Event | None" = None
        self._lock = threading.Lock()
        self._devices: dict = {}               # uuid -> pb.DeviceDescriptor
        self._id2uuid: dict = {}               # device-id (instance_id OR data_id) -> uuid

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="hub-agent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._do_stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _do_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._outq is not None:
            self._outq.put_nowait(None)        # unblock the out generator

    # -- public API (any thread) --------------------------------------------
    def announce(self, descriptor) -> None:
        pd = convert.descriptor_to_proto(descriptor)
        with self._lock:
            self._devices[pd.uuid] = pd
            # Readings are stamped with the device's *data_id* (= uuid once
            # onboarded, else the instance_id), not necessarily the instance_id —
            # so register both forms, mapping to the wire uuid.
            self._id2uuid[pd.instance_id] = pd.uuid
            self._id2uuid[pd.uuid] = pd.uuid
        self._send(pb.AgentMessage(announce=pd))

    def retire(self, key: str) -> None:
        """Retire by instance_id or uuid."""
        with self._lock:
            uuid = self._id2uuid.pop(key, key)
            self._devices.pop(uuid, None)
            for inst, u in list(self._id2uuid.items()):
                if u == uuid:
                    self._id2uuid.pop(inst, None)
        self._send(pb.AgentMessage(retire=pb.Retire(device_uuid=uuid)))

    def set_devices(self, descriptors) -> None:
        """Reconcile the published set: announce new, retire vanished."""
        wanted = {convert.descriptor_to_proto(d).uuid: d for d in descriptors}
        with self._lock:
            current = set(self._devices)
        for uuid in current - set(wanted):
            self.retire(uuid)
        for d in wanted.values():
            self.announce(d)

    def feed(self, readings) -> None:
        """Publish a batch of app Readings. r.device is the device's data_id
        (= uuid once onboarded), resolved to the wire uuid via _id2uuid."""
        with self._lock:
            i2u = dict(self._id2uuid)
        out = [convert.reading_to_proto(r, i2u[r.device])
               for r in readings if r.device in i2u]
        if out:
            self._send(pb.AgentMessage(readings=pb.ReadingBatch(readings=out)))

    # -- internals -----------------------------------------------------------
    def _send(self, msg) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._safe_put, msg)

    def _safe_put(self, msg) -> None:
        try:
            self._outq.put_nowait(msg)
        except Exception:
            pass

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
        self._outq = asyncio.Queue()
        self._stop = asyncio.Event()
        self._loop = loop                      # set last → _send sees a ready queue
        try:
            loop.run_until_complete(self._session_loop())
        finally:
            _drain(loop)
            loop.close()

    async def _session_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with grpc.aio.insecure_channel(self._addr) as ch:
                    stub = rpc.IngestStub(ch)
                    call = stub.Session(self._outgen())
                    async for _hub_msg in call:
                        pass                   # M1: down channel unused
            except asyncio.CancelledError:
                break
            except Exception as e:             # hub down / connection lost
                self._notify(False, f"hub disconnected: {e}")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass                           # backoff, then reconnect

    async def _outgen(self):
        yield pb.AgentMessage(hello=pb.Hello(
            agent_id=self._agent_id, contract_version=CONTRACT_VERSION))
        with self._lock:
            descs = list(self._devices.values())
        for d in descs:                        # (re)announce on every (re)connect
            yield pb.AgentMessage(announce=d)
        self._notify(True, f"connected to {self._addr}")
        while True:
            msg = await self._outq.get()
            if msg is None or self._stop.is_set():
                break
            yield msg
