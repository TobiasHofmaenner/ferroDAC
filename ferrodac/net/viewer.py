"""HubViewer — consume a hub's devices + live readings.

Watches the catalog (remote devices) and subscribes to their readings, handing
both to callbacks. The Qt side turns catalog events into device ports (§6.1
'bind REMOTE') and feeds the readings into the Engine, so remote devices render
exactly like local ones. Runs grpc.aio in its own thread; callbacks fire on that
thread (marshal to the GUI thread on the Qt side).

Live VIDEO (§9) rides a separate explicit stream: `set_frame_refs({(uuid,
source_id), ...})` opens/replaces a WatchFrames subscription for exactly the
cameras routed to a local panel — opening it is the demand signal that makes
the remote agent start encoding, closing it stops the camera's wire traffic.
"""

from __future__ import annotations

import asyncio
import logging

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import call_soon_safe, convert, watch_connectivity
from .session import ReconnectingClient

log = logging.getLogger("hub.viewer")


class HubViewer(ReconnectingClient):
    _thread_name = "hub-viewer"
    _disconnect_label = "hub"

    def __init__(self, addr: str, on_catalog=None, on_readings=None,
                 on_state=None):
        super().__init__(addr, on_state)       # thread / loop / stop / reconnect FSM
        self._on_catalog = on_catalog          # (event_type: str, pb.DeviceDescriptor)
        self._on_readings = on_readings        # (list[app Reading]) — also frames
        self._frame_refs: set = set()          # wanted {(uuid, source_id)}
        self._frames_task = None               # the live WatchFrames task
        self._stub = None                      # this session's ViewerStub

    # -- public API (any thread) ----------------------------------------------
    def send_command(self, device_uuid, sink_id, value, on_result=None) -> None:
        """Set a control sink on a device the hub knows (§5.3). NON-BLOCKING: the
        SendCommand RPC runs on the viewer loop and `on_result(ok: bool, detail:
        str)` fires on that thread when the hub relays the agent's Ack (the Qt side
        marshals to the GUI). Control must never freeze the UI. `value`: bool→TOGGLE,
        number→SETPOINT, str→ENUM, None→ACTION trigger."""
        call_soon_safe(self._loop, self._do_send_command,
                       str(device_uuid), str(sink_id), value, on_result)

    def _do_send_command(self, device_uuid, sink_id, value, on_result) -> None:
        if self._stub is None:
            if on_result is not None:
                on_result(False, "not connected to the hub")
            return
        req = pb.CommandRequest(device_uuid=device_uuid, sink_id=sink_id)
        self._set_command_value(req, value)
        stub = self._stub

        async def go():
            try:
                ack = await stub.SendCommand(req)
                res = (bool(ack.ok), str(ack.detail or ""))
            except Exception as exc:           # noqa: BLE001 — surface to the caller
                res = (False, str(exc))
            if on_result is not None:
                on_result(*res)

        asyncio.ensure_future(go())

    def add_remote_device(self, agent_id, instance_id, on_result=None) -> None:
        """Ask a client (by agent_id) to onboard one of its AVAILABLE devices. Non-
        blocking; on_result(ok, detail) on the loop when the client Acks."""
        call_soon_safe(self._loop, self._do_add_remote,
                       str(agent_id), str(instance_id), on_result)

    def _do_add_remote(self, agent_id, instance_id, on_result) -> None:
        if self._stub is None:
            if on_result is not None:
                on_result(False, "not connected to the hub")
            return
        req = pb.AddDeviceRequest(agent_id=agent_id, instance_id=instance_id)
        stub = self._stub

        async def go():
            try:
                ack = await stub.AddRemoteDevice(req)
                res = (bool(ack.ok), str(ack.detail or ""))
            except Exception as exc:           # noqa: BLE001
                res = (False, str(exc))
            if on_result is not None:
                on_result(*res)

        asyncio.ensure_future(go())

    def remove_remote_device(self, device_uuid, on_result=None) -> None:
        """Ask the owning client (by device uuid) to retire an active remote device —
        the reverse of add_remote_device. Non-blocking; on_result(ok, detail) on the
        loop when the owner Acks."""
        call_soon_safe(self._loop, self._do_remove_remote,
                       str(device_uuid), on_result)

    def _do_remove_remote(self, device_uuid, on_result) -> None:
        if self._stub is None:
            if on_result is not None:
                on_result(False, "not connected to the hub")
            return
        req = pb.RemoveDeviceRequest(device_uuid=device_uuid)
        stub = self._stub

        async def go():
            try:
                ack = await stub.RemoveRemoteDevice(req)
                res = (bool(ack.ok), str(ack.detail or ""))
            except Exception as exc:           # noqa: BLE001
                res = (False, str(exc))
            if on_result is not None:
                on_result(*res)

        asyncio.ensure_future(go())

    def set_config(self, device_uuid, *, option=None, rate_hz=None, rename=None,
                   on_result=None) -> None:
        """Configure a hub device (§5.3) — pass exactly ONE of option=(key, value),
        rate_hz=float, rename=str. Non-blocking; on_result(ok, detail) on the loop."""
        call_soon_safe(self._loop, self._do_set_config, str(device_uuid),
                       option, rate_hz, rename, on_result)

    def _do_set_config(self, device_uuid, option, rate_hz, rename, on_result) -> None:
        if self._stub is None:
            if on_result is not None:
                on_result(False, "not connected to the hub")
            return
        req = pb.ConfigureRequest(device_uuid=device_uuid)
        if option is not None:
            req.option.key, req.option.value = str(option[0]), str(option[1])
        elif rate_hz is not None:
            req.rate_hz = float(rate_hz)
        elif rename is not None:
            req.rename = str(rename)
        stub = self._stub

        async def go():
            try:
                ack = await stub.SetConfig(req)
                res = (bool(ack.ok), str(ack.detail or ""))
            except Exception as exc:           # noqa: BLE001 — surface to the caller
                res = (False, str(exc))
            if on_result is not None:
                on_result(*res)

        asyncio.ensure_future(go())

    @staticmethod
    def _set_command_value(req, value) -> None:
        """Fill the CommandRequest value oneof. bool BEFORE int (bool subclasses int)."""
        if value is None:
            req.trigger = True                 # ACTION sink — no value
        elif isinstance(value, bool):
            req.boolean = value
        elif isinstance(value, (int, float)):
            req.scalar = float(value)
        else:
            req.text = str(value)

    def set_frame_refs(self, refs: set) -> None:
        """Replace the watched camera set. Thread-safe; a no-op when unchanged.
        The stream restarts with the new refs (grpc has no re-subscribe on a
        live stream); an empty set just closes it."""
        refs = {(str(u), str(s)) for (u, s) in refs}
        call_soon_safe(self._loop, self._apply_frame_refs, refs)

    # -- session ---------------------------------------------------------------
    async def _run_session(self, ch) -> None:
        v = rpc.ViewerStub(ch)
        self._stub = v
        # REAL link from the channel state (not 'we opened a channel'); watch the
        # catalog + subscribe to readings until any ends (disconnect) or we stop.
        conn = asyncio.create_task(watch_connectivity(ch, self._addr, self._notify))
        watch = asyncio.create_task(self._watch(v))
        sub = asyncio.create_task(self._subscribe(v))
        stopper = asyncio.create_task(self._stop.wait())
        if self._frame_refs:                   # resume watched cameras on reconnect
            self._start_frames()
        try:
            await asyncio.wait({conn, watch, sub, stopper},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stub = None
            tasks = [conn, watch, sub, stopper]
            if self._frames_task is not None:
                tasks.append(self._frames_task)
                self._frames_task = None
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _apply_frame_refs(self, refs: set) -> None:
        if refs == self._frame_refs:
            return
        self._frame_refs = refs
        self._start_frames()

    def _start_frames(self) -> None:
        """(Re)open the WatchFrames stream for the current refs (loop thread)."""
        if self._frames_task is not None:
            self._frames_task.cancel()
            self._frames_task = None
        if not self._frame_refs or self._stub is None:
            return
        req = pb.SubscribeRequest(sources=[
            pb.SourceRef(device_uuid=u, source_id=s)
            for (u, s) in sorted(self._frame_refs)])
        self._frames_task = asyncio.ensure_future(self._frames(self._stub, req))

    async def _frames(self, v, req) -> None:
        try:
            async for batch in v.WatchFrames(req):
                if self._on_readings is not None and batch.readings:
                    self._on_readings(
                        [convert.reading_from_proto(r) for r in batch.readings])
        except asyncio.CancelledError:
            raise
        except Exception:                      # noqa: BLE001 — stream died; the
            log.debug("WatchFrames ended", exc_info=True)   # next reconnect (or
        #                                        set_frame_refs) reopens it

    async def _watch(self, v) -> None:
        async for ev in v.WatchCatalog(pb.CatalogRequest()):
            if self._on_catalog is not None:
                self._on_catalog(pb.CatalogEvent.Type.Name(ev.type), ev.device)

    async def _subscribe(self, v) -> None:
        async for batch in v.Subscribe(pb.SubscribeRequest()):
            if self._on_readings is not None and batch.readings:
                self._on_readings(
                    [convert.reading_from_proto(r) for r in batch.readings])
