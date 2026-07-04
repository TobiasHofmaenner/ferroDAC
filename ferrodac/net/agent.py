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

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import CONTRACT_VERSION, call_soon_safe, convert, watch_connectivity
from .session import ReconnectingClient

log = logging.getLogger("hub.agent")


class HubAgent(ReconnectingClient):
    _thread_name = "hub-agent"
    _disconnect_label = "hub"

    def __init__(self, addr: str, agent_id: str = "ferrodac", on_state=None):
        super().__init__(addr, on_state)       # thread / loop / stop / reconnect FSM
        self._agent_id = agent_id
        self._outq: "asyncio.Queue | None" = None
        self._lock = threading.Lock()
        self._devices: dict = {}               # uuid -> pb.DeviceDescriptor
        self._id2uuid: dict = {}               # device-id (instance_id OR data_id) -> uuid

    def _on_loop_created(self, loop) -> None:
        self._outq = asyncio.Queue()           # so _send has a queue before session 1

    def _do_stop(self) -> None:
        super()._do_stop()                     # set the stop event…
        if self._outq is not None:
            self._outq.put_nowait(None)        # …and unblock the out generator

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
        call_soon_safe(self._loop, self._safe_put, msg)

    def _safe_put(self, msg) -> None:
        try:
            self._outq.put_nowait(msg)
        except Exception:
            pass

    async def _run_session(self, ch) -> None:
        # A FRESH queue per session: readings that piled up while offline die with
        # the old queue (they're the live view only — durability is the store sync),
        # and a half-cancelled previous generator can never steal from the new one.
        self._outq = asyncio.Queue()
        # report the REAL link from the channel state, not from the outgoing
        # generator running (which fires even when the hub is down)
        watcher = asyncio.ensure_future(
            watch_connectivity(ch, self._addr, self._notify))
        try:
            call = rpc.IngestStub(ch).Session(self._outgen())
            async for _hub_msg in call:
                pass                           # M1: down channel unused
        finally:
            watcher.cancel()

    async def _outgen(self):
        outq = self._outq                      # pin THIS session's queue (a late-
        #                                        cancelled generator must not read
        #                                        a successor session's queue)
        yield pb.AgentMessage(hello=pb.Hello(
            agent_id=self._agent_id, contract_version=CONTRACT_VERSION))
        with self._lock:
            descs = list(self._devices.values())
        for d in descs:                        # (re)announce on every (re)connect
            yield pb.AgentMessage(announce=d)
        while True:                            # link state comes from watch_connectivity
            msg = await outq.get()
            if msg is None or self._stop.is_set():
                break
            yield msg
