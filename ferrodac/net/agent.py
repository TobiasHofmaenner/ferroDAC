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

    def __init__(self, addr: str, agent_id: str = "ferrodac", on_state=None,
                 on_frame_demand=None, on_command=None, on_configure=None,
                 on_add_device=None, on_remove_device=None, on_prompt_response=None):
        super().__init__(addr, on_state)       # thread / loop / stop / reconnect FSM
        self._agent_id = agent_id
        self._outq: "asyncio.Queue | None" = None
        self._lock = threading.Lock()
        self._devices: dict = {}               # uuid -> pb.DeviceDescriptor
        self._id2uuid: dict = {}               # device-id (instance_id OR data_id) -> uuid
        # §7.3 open-prompt mirror: id -> pb.WirePrompt for every prompt WE published
        # that has not been closed. Replayed by _outgen on every (re)connect — the hub
        # closes an agent's prompts when its session drops, so without the replay a
        # network blip / hub restart left viewers blind to still-open prompts. Only
        # ever fed by publish_prompt (the app publishes LOCAL prompts only), so a
        # remote-injected prompt can never echo back through here.
        self._open_prompts: dict = {}
        # (uuid, source_id, active) — viewers started/stopped watching a camera
        # (§9). Called on the AGENT's asyncio thread; the wirer marshals.
        self._on_frame_demand = on_frame_demand
        # control (§5.3): (device_uuid, sink_id, value) -> (ok: bool, detail: str).
        # Runs the local device write; called off the agent loop (may block on I/O).
        self._on_command = on_command
        # configure (§5.3): (device_uuid, action, *args) -> (ok, detail), where action
        # is "option"/(key,value), "rate"/(hz,) or "rename"/(name,). Off-loop too.
        self._on_configure = on_configure
        # remote device addition: (instance_id) -> (ok, detail). Onboards a local
        # available device on request from another client. Off the agent loop.
        self._on_add_device = on_add_device
        # remote device removal: (instance_id) -> (ok, detail). Retires a local active
        # device on request from another client (reverse of add). Off the agent loop.
        self._on_remove_device = on_remove_device
        # interaction §7.3: a viewer answered one of THIS agent's prompts →
        # (prompt_id, answer, by). The wirer resolves it in the local store (→ the
        # driver's on_response → RESPOND to the device). first-responder-wins in the store.
        self._on_prompt_response = on_prompt_response
        self.demanded_frames: set = set()      # {(uuid, source_id)} currently watched

    def _on_loop_created(self, loop) -> None:
        self._outq = asyncio.Queue()           # so _send has a queue before session 1

    def _do_stop(self) -> None:
        super()._do_stop()                     # set the stop event…
        if self._outq is not None:
            self._outq.put_nowait(None)        # …and unblock the out generator

    # -- public API (any thread) --------------------------------------------
    def announce(self, descriptor) -> None:
        self._announce_proto(convert.descriptor_to_proto(descriptor))

    def _announce_proto(self, pd) -> None:
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

    def set_devices(self, descriptors, available=()) -> None:
        """Reconcile the published set. ACTIVE devices announce as-is; AVAILABLE
        devices (advertised for remote addition) are tagged `available` + `agent_id`
        with a synthetic uuid, so another client can see and onboard them. Announce
        new, retire vanished — one call carries the whole current picture."""
        wanted = {}
        for d in descriptors:
            pd = convert.descriptor_to_proto(d, available=False, agent_id=self._agent_id)
            wanted[pd.uuid] = pd
        for d in available:
            pd = convert.descriptor_to_proto(d, available=True, agent_id=self._agent_id)
            wanted[pd.uuid] = pd
        with self._lock:
            current = dict(self._devices)
        for uuid in set(current) - set(wanted):
            self.retire(uuid)
        for uuid, pd in wanted.items():
            prev = current.get(uuid)
            if prev is None or prev != pd:      # announce only NEW or genuinely
                self._announce_proto(pd)        # CHANGED descriptors — an unchanged
            #   set was re-broadcast to every viewer on each active_changed (fired
            #   ~2 s by a single device's flapping sink readback → whole-catalog churn)

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
            async for hub_msg in call:
                which = hub_msg.WhichOneof("msg")
                if which == "frame_demand":
                    fd = hub_msg.frame_demand
                    ref = (fd.device_uuid, fd.source_id)
                    if fd.active:
                        self.demanded_frames.add(ref)
                    else:
                        self.demanded_frames.discard(ref)
                    if self._on_frame_demand is not None:
                        try:
                            self._on_frame_demand(fd.device_uuid, fd.source_id,
                                                  fd.active)
                        except Exception:      # noqa: BLE001 — a bad callback
                            log.debug("frame-demand callback failed",
                                      exc_info=True)
                elif which == "command":
                    await self._handle_command(hub_msg.command)
                elif which == "configure":
                    await self._handle_configure(hub_msg.configure)
                elif which == "add_device":
                    await self._handle_add_device(hub_msg.add_device)
                elif which == "remove_device":
                    await self._handle_remove_device(hub_msg.remove_device)
                elif which == "prompt_response":
                    self._handle_prompt_response(hub_msg.prompt_response)
                # welcome: nothing to do
        finally:
            self.demanded_frames.clear()       # a reconnect re-sends active demand
            watcher.cancel()

    async def _handle_command(self, cmd) -> None:
        """Execute a hub command on the local device and Ack the result (§5.3). The
        write may BLOCK on device I/O, so it runs off the agent loop (a physicist's
        control must never stall the session). The actual effect is captured by the
        sink's readback source (§7.5), not this Ack — the Ack reports acceptance."""
        value = self._command_value(cmd)
        ok, detail = False, "no command handler on this agent"
        if self._on_command is not None:
            try:
                ok, detail = await asyncio.to_thread(
                    self._on_command, cmd.device_uuid, cmd.sink_id, value)
            except Exception as exc:           # noqa: BLE001 — surface it in the Ack
                ok, detail = False, str(exc)
        self._send(pb.AgentMessage(ack=pb.Ack(
            request_id=cmd.request_id, ok=bool(ok), detail=str(detail or ""))))

    def _handle_prompt_response(self, pr) -> None:
        """A viewer answered one of THIS agent's prompts (§7.3) → hand the answer to the
        wirer, which resolves it in the local store (→ the driver's on_response → RESPOND to
        the device). first-responder-wins is arbitrated there; an answer for a gone prompt
        no-ops. Runs on the agent loop — the callback marshals to the GUI thread."""
        if self._on_prompt_response is None:
            return
        which = pr.WhichOneof("value")
        if which == "boolean":
            answer = pr.boolean
        elif which == "text":
            answer = pr.text
        else:
            answer = True                          # trigger → acknowledge
        try:
            self._on_prompt_response(pr.id, answer, pr.by)
        except Exception:                          # noqa: BLE001 — a bad callback ≠ break session
            log.debug("prompt-response callback failed", exc_info=True)

    # -- interaction prompts (§7.3): mirror opens/closes to the hub -----------
    def publish_prompt(self, wire) -> None:
        """Mirror an OPEN device prompt (a pb.WirePrompt) up to the hub so viewers see it.
        Also recorded in the open-prompt mirror, which _outgen replays on every
        (re)connect — so a prompt published while offline / before the first session
        still reaches the hub, and a reconnect re-opens what the hub closed."""
        with self._lock:
            self._open_prompts[wire.id] = wire
        self._send(pb.AgentMessage(prompt=pb.PromptEvent(opened=wire)))

    def close_prompt(self, prompt_id: str, answer_text: str = "", by: str = "") -> None:
        """Mirror a prompt RESOLUTION up to the hub → every viewer withdraws it. Drops
        it from the replay mirror too, so a prompt resolved while offline is never
        resurrected on the next (re)connect."""
        with self._lock:
            self._open_prompts.pop(str(prompt_id), None)
        self._send(pb.AgentMessage(prompt=pb.PromptEvent(closed=pb.PromptClosed(
            id=str(prompt_id), answer_text=str(answer_text), by=str(by)))))

    @staticmethod
    def _command_value(cmd):
        """The value the viewer set, unwrapped from the proto oneof. A `trigger`
        (ACTION sink) carries no value → None."""
        which = cmd.WhichOneof("value")
        if which == "scalar":
            return cmd.scalar
        if which == "boolean":
            return cmd.boolean
        if which == "text":
            return cmd.text
        return None                            # trigger (ACTION) or unset

    async def _handle_configure(self, cfg) -> None:
        """Apply a hub Configure (option/rate/rename) to the local device and Ack
        (§5.3). The new state returns to the viewer on the re-announced descriptor."""
        action, args = self._configure_action(cfg)
        ok, detail = False, "no configure handler on this agent"
        if action is None:
            ok, detail = False, "empty configure action"
        elif self._on_configure is not None:
            try:
                ok, detail = await asyncio.to_thread(
                    self._on_configure, cfg.device_uuid, action, *args)
            except Exception as exc:           # noqa: BLE001 — surface it in the Ack
                ok, detail = False, str(exc)
        self._send(pb.AgentMessage(ack=pb.Ack(
            request_id=cfg.request_id, ok=bool(ok), detail=str(detail or ""))))

    async def _handle_add_device(self, msg) -> None:
        """Another client asked us to onboard one of our AVAILABLE devices → run the
        local add and Ack. The device then re-announces as active on its own."""
        ok, detail = False, "no add-device handler on this agent"
        if self._on_add_device is not None:
            try:
                ok, detail = await asyncio.to_thread(self._on_add_device,
                                                     msg.instance_id)
            except Exception as exc:           # noqa: BLE001 — surface it in the Ack
                ok, detail = False, str(exc)
        self._send(pb.AgentMessage(ack=pb.Ack(
            request_id=msg.request_id, ok=bool(ok), detail=str(detail or ""))))

    async def _handle_remove_device(self, msg) -> None:
        """Another client asked us to retire one of our ACTIVE devices → run the local
        remove and Ack. The device then drops from the catalog. Reverse of add-device."""
        ok, detail = False, "no remove-device handler on this agent"
        if self._on_remove_device is not None:
            try:
                ok, detail = await asyncio.to_thread(self._on_remove_device,
                                                     msg.instance_id)
            except Exception as exc:           # noqa: BLE001 — surface it in the Ack
                ok, detail = False, str(exc)
        self._send(pb.AgentMessage(ack=pb.Ack(
            request_id=msg.request_id, ok=bool(ok), detail=str(detail or ""))))

    @staticmethod
    def _configure_action(cfg):
        which = cfg.WhichOneof("action")
        if which == "option":
            return "option", (cfg.option.key, cfg.option.value)
        if which == "rate_hz":
            return "rate", (cfg.rate_hz,)
        if which == "rename":
            return "rename", (cfg.rename,)
        return None, ()

    async def _outgen(self):
        outq = self._outq                      # pin THIS session's queue (a late-
        #                                        cancelled generator must not read
        #                                        a successor session's queue)
        yield pb.AgentMessage(hello=pb.Hello(
            agent_id=self._agent_id, contract_version=CONTRACT_VERSION))
        with self._lock:
            descs = list(self._devices.values())
            prompts = list(self._open_prompts.values())
        for d in descs:                        # (re)announce on every (re)connect
            yield pb.AgentMessage(announce=d)
        for w in prompts:                      # …and re-open still-open prompts (§7.3):
            #                                    the hub closed them when the previous
            #                                    session dropped; viewers re-inject
            #                                    idempotently (dedup by id).
            yield pb.AgentMessage(prompt=pb.PromptEvent(opened=w))
        while True:                            # link state comes from watch_connectivity
            msg = await outq.get()
            if msg is None or self._stop.is_set():
                break
            yield msg
