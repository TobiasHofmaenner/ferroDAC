"""HubPromptWatch — the VIEWER side of the interaction prompt relay (DESIGN §7.3).

A device prompt is raised + owned by the AGENT (it holds the driver's on_response and the
device link). The agent publishes its open/closed prompts up its Ingest session (HubAgent);
this client is what a VIEWER runs to SEE those prompts (WatchPrompts: snapshot of open, then
live) and ANSWER them (RespondPrompt → routed by the hub down to the owning agent). Runs
grpc.aio in its own thread, like HubTagSync; callbacks fire on that thread and the Qt glue
marshals to the GUI.
"""

from __future__ import annotations

import logging

import asyncio

from ferrodac_contract.v1 import data_plane_pb2 as pb

from ..core.interaction import STAY, Prompt
from .session import ReconnectingClient
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

log = logging.getLogger("hub.prompts")


def prompt_to_wire(prompt) -> "pb.WirePrompt":
    """A core.interaction.Prompt → the wire form the agent publishes."""
    return pb.WirePrompt(
        id=prompt.id, device_uuid=str(prompt.device_id), question=prompt.question,
        kind=prompt.kind, title=prompt.title, options=[str(o) for o in prompt.options],
        severity=prompt.severity, timeout=float(prompt.timeout or 0.0),
        on_timeout=str(prompt.on_timeout), created=float(prompt.created or 0.0))


def prompt_from_wire(wire) -> "Prompt":
    """A WirePrompt → a Prompt for a VIEWER's inbox. The viewer never owns the timeout (the
    agent/device does), so timeout is dropped + on_timeout=STAY — the viewer must never
    auto-resolve; it only shows the request and relays an answer."""
    return Prompt(
        device_id=wire.device_uuid, question=wire.question, kind=wire.kind, id=wire.id,
        title=wire.title, options=list(wire.options), severity=wire.severity,
        timeout=None, on_timeout=STAY, created=wire.created or 0.0)


class HubPromptWatch(ReconnectingClient):
    _thread_name = "hub-prompts"
    _disconnect_label = "prompt sync"

    def __init__(self, addr: str, on_open=None, on_close=None, on_state=None):
        super().__init__(addr, on_state)       # thread / loop / stop / reconnect FSM
        self._on_open = on_open                # (WirePrompt) — a remote open prompt appeared
        self._on_close = on_close              # (id) — it resolved anywhere → withdraw
        self._stub = None

    # -- public API (any thread) --------------------------------------------
    def respond(self, prompt_id: str, answer, by: str = "", on_result=None) -> None:
        """Answer a remote prompt → RespondPrompt, which the hub routes to the owning agent.

        ``on_result(ok, detail)`` (optional) reports the Ack, fired on the watch
        thread — the Qt glue must marshal. Three-valued ``ok``:
          * True  — the hub accepted + routed the answer to the owning agent;
          * False — the hub REJECTED it (the prompt is gone: answered elsewhere /
                    owner disconnected) — authoritative, the answer went nowhere;
          * None  — no Ack at all (channel down / hub unreachable) — the answer
                    went nowhere, but the prompt may still be open.
        """
        req = pb.RespondPromptRequest(id=str(prompt_id), by=str(by))
        if isinstance(answer, bool):
            req.boolean = answer               # confirm / acknowledge (True)
        elif answer is None:
            req.trigger = True                 # a bare action
        else:
            req.text = str(answer)             # choice option / text
        if not self._schedule(self._respond(req, on_result)):
            self._report(on_result, None, "prompt channel not running")

    # -- internals -----------------------------------------------------------
    def _schedule(self, coro) -> bool:
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: loop.create_task(coro))
                return True
            except RuntimeError:               # loop already closed — worker is gone
                pass
        coro.close()                           # not running (yet) — nothing will Ack
        return False

    @staticmethod
    def _report(on_result, ok, detail) -> None:
        if on_result is None:
            return
        try:
            on_result(ok, detail)
        except Exception:                      # a bad observer must never break the loop
            log.debug("respond on_result callback failed", exc_info=True)

    async def _respond(self, req, on_result=None) -> None:
        """Send the answer and report the Ack. A dropped/failed relay is no longer
        silent: the viewer keeps/restores the card on a bad result (the answer went
        nowhere) — a still-open prompt also re-surfaces on the reconnect snapshot."""
        stub = self._stub
        if stub is None:                       # between sessions — no hub to hear us
            self._report(on_result, None, "prompt channel disconnected")
            return
        try:
            ack = await stub.RespondPrompt(req)
        except Exception as e:                 # transport failure — no Ack ever came
            self._report(on_result, None, str(e) or "hub unreachable")
            return
        self._report(on_result, bool(ack.ok), ack.detail or "")

    async def _run_session(self, ch) -> None:
        self._stub = rpc.ViewerStub(ch)
        self._notify(True, f"prompt sync connected to {self._addr}")
        try:
            watch = asyncio.create_task(self._watch())
            stopper = asyncio.create_task(self._stop.wait())
            await asyncio.wait({watch, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for t in (watch, stopper):
                t.cancel()
            await asyncio.gather(watch, stopper, return_exceptions=True)
        finally:
            self._stub = None

    async def _watch(self) -> None:
        async for ev in self._stub.WatchPrompts(pb.WatchPromptsRequest()):
            which = ev.WhichOneof("ev")
            if which == "opened" and self._on_open is not None:
                self._on_open(ev.opened)
            elif which == "closed" and self._on_close is not None:
                self._on_close(ev.closed.id)
